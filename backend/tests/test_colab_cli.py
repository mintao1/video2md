"""Real subprocess/file transfer contracts; only the Google CLI boundary is fake."""
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from video2pdf import artsink, colab
from video2pdf.clips import remote_cut_clips
from video2pdf.frames import ocr_candidates
from video2pdf.transcribe import transcribe


@pytest.fixture
def cli(monkeypatch, tmp_path):
    binary = Path(__file__).parent / "fixtures/fake_colab_cli.py"
    for key, value in {
        "V2PDF_COLAB_BIN": str(binary), "V2PDF_COLAB_LOCK": str(tmp_path / "session.lock"),
        "V2PDF_COLAB_SESSION": "isolated-test", "V2PDF_REMOTE": "colab-cli",
        "V2PDF_OCR_REMOTE": "colab-cli", "V2PDF_CLIPS_REMOTE": "colab-cli",
        "V2PDF_CLIPS_SOURCE": "local", "V2PDF_COLAB_CPU_FALLBACK": "off",
    }.items():
        monkeypatch.setitem(colab.ENV, key, value)
    monkeypatch.setenv("FAKE_COLAB_ROOT", str(tmp_path / "remote"))
    monkeypatch.setattr(colab.machinery.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(artsink.config, "ARTIFACT_MODE", "vault")
    attach = tmp_path / "attachments"
    attach.mkdir()
    monkeypatch.setattr(artsink.config, "ATTACH_DIR", attach)
    audio = tmp_path / "input.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\0\0" * 16000)
    return binary, audio, tmp_path


def sessions(binary):
    return subprocess.check_output([sys.executable, str(binary), "sessions"], text=True)


def test_direct_cli_transcription_ocr_clips_and_session_cleanup(cli):
    binary, audio, tmp = cli
    image = tmp / "frame.jpg"
    image.write_bytes(b"jpeg-fixture")
    with colab.session_scope():
        segments, confidence, dropped = transcribe(audio, "tiny", "en", tmp, "colab-cli")
        assert segments[0]["text"] == "CLI speech" and confidence > 0.9 and dropped == 0
        assert "isolated-test" in sessions(binary)
        items, raw = ocr_candidates([(1.0, image)], "remote", "colab-cli")
        assert items == [(1.0, "CLI OCR", 0.99)] and raw[0][3] is None
        result = remote_cut_clips([(image.name, "00:01")], audio, 3.0, [], tmp, "colab-cli")
        assert result[image.name]
        assert (artsink.attach_dir() / result[image.name]).read_bytes() == b"offline-clip-fixture"
    assert "No active sessions" in sessions(binary)


def test_connected_tenant_routes_all_gpu_work_through_its_own_cli(cli, monkeypatch):
    binary, audio, tmp = cli
    monkeypatch.setitem(colab.ENV, "V2PDF_COLAB_USERS_DIR", str(tmp / "tenants"))
    directory = colab.credential_directory("alice")
    directory.mkdir(parents=True)
    (directory / "token.json").write_text("offline-tenant-token")
    image = tmp / "tenant-frame.jpg"
    image.write_bytes(b"jpeg-fixture")
    original = subprocess.run
    seen_homes = []

    def record(*args, **kwargs):
        seen_homes.append(kwargs.get("env", {}).get("HOME"))
        return original(*args, **kwargs)

    monkeypatch.setattr(colab.machinery.subprocess, "run", record)
    with colab.session_scope(owner="alice"):
        assert colab.remote_for_user("https://shared-pool.invalid") == "colab-cli"
        assert transcribe(audio, "tiny", "en", tmp, "https://shared-pool.invalid")[0][0]["text"] == "CLI speech"
        assert ocr_candidates([(1.0, image)], "remote", "https://shared-pool.invalid")[0]
        assert remote_cut_clips([(image.name, "00:01")], audio, 3.0, [], tmp, "https://shared-pool.invalid")[image.name]
    cli_homes = [home for home in seen_homes if home]
    assert cli_homes and set(cli_homes) == {str(colab.tenant_home("alice"))}
    assert not (tmp / "remote" / "active").exists()
    assert colab.remote_for_user("https://shared-pool.invalid") == "https://shared-pool.invalid"
    assert colab.machinery.CLI_HOME == ""


def test_two_tenants_use_distinct_cli_homes_and_session_names(cli, monkeypatch):
    _, audio, tmp = cli
    monkeypatch.setitem(colab.ENV, "V2PDF_COLAB_USERS_DIR", str(tmp / "tenants"))
    observed = []
    for uid in ("alice", "bob"):
        directory = colab.credential_directory(uid)
        directory.mkdir(parents=True)
        (directory / "token.json").write_text("offline-token-" + uid)
        with colab.session_scope(owner=uid):
            colab._work(lambda: observed.append((colab.machinery.CLI_HOME, colab.machinery.SESSION)))
    assert observed[0][0] != observed[1][0]
    assert observed[0][1] != observed[1][1]
    assert (colab.credential_directory("alice") / "token.json").read_text() == "offline-token-alice"


def test_tenant_identity_propagates_into_graph_node_threads(cli, monkeypatch):
    from langgraph.graph import END, START, StateGraph

    _, _, tmp = cli
    monkeypatch.setitem(colab.ENV, "V2PDF_COLAB_USERS_DIR", str(tmp / "tenants"))
    directory = colab.credential_directory("alice")
    directory.mkdir(parents=True)
    (directory / "token.json").write_text("offline-token")
    observed = []

    def gpu(state):
        assert colab.remote_for_user("shared-pool") == "colab-cli"
        colab._work(lambda: observed.append(colab.machinery.CLI_HOME))
        return {}

    builder = StateGraph(dict)
    builder.add_node("gpu", gpu)
    builder.add_edge(START, "gpu")
    builder.add_edge("gpu", END)
    with colab.session_scope(owner="alice"):
        builder.compile().invoke({})
    assert observed == [str(colab.tenant_home("alice"))]
    assert colab._OWNER.get() == "" and colab._USE.get() is None


def test_failed_tenant_directory_creation_restores_context(cli, monkeypatch):
    _, _, tmp = cli
    occupied = tmp / "not-a-directory"
    occupied.write_text("occupied")
    monkeypatch.setitem(colab.ENV, "V2PDF_COLAB_USERS_DIR", str(occupied))
    with pytest.raises(OSError), colab._session(owner="alice"):
        pytest.fail("must not execute when the credential directory cannot be created")
    assert colab._OWNER.get() == "" and colab.machinery.CLI_HOME == ""


def test_unconnected_tenant_cannot_use_a_service_token(cli, monkeypatch):
    _, _, tmp = cli
    monkeypatch.setitem(colab.ENV, "V2PDF_COLAB_USERS_DIR", str(tmp / "tenants"))
    shared = tmp / "shared-home"
    token = shared / ".config" / "colab-cli" / "token.json"
    token.parent.mkdir(parents=True)
    token.write_text("service-token")
    monkeypatch.setattr(colab.Path, "home", classmethod(lambda cls: shared))
    called = []
    with colab.session_scope(owner="unconnected"):
        with pytest.raises(RuntimeError, match="当前用户尚未连接 Colab"):
            colab._work(lambda: called.append(True))
    assert called == [] and token.read_text() == "service-token"


def test_a_run_cannot_change_tenants_or_routes_midway(cli, monkeypatch):
    _, _, tmp = cli
    monkeypatch.setitem(colab.ENV, "V2PDF_COLAB_USERS_DIR", str(tmp / "tenants"))
    with colab.session_scope(owner="alice"):
        assert colab.remote_for_user("pool") == "pool"
        with pytest.raises(RuntimeError, match="不能切换租户"), colab.session_scope(owner="bob"):
            pass
        directory = colab.credential_directory("alice")
        directory.mkdir(parents=True)
        (directory / "token.json").write_text("new-token")
        assert colab.remote_for_user("pool") == "pool"
    with colab.session_scope(owner="alice"):
        assert colab.remote_for_user("pool") == "colab-cli"


def test_cli_failure_propagates_and_releases_session(cli, monkeypatch):
    binary, audio, tmp = cli
    monkeypatch.setenv("FAKE_COLAB_FAIL_EXEC", "1")
    with pytest.raises(RuntimeError, match="remote worker failed"), colab.session_scope():
        transcribe(audio, "tiny", "en", tmp, "colab-cli")
    assert "No active sessions" in sessions(binary)


def test_gpu_unavailable_does_not_silently_fall_back_to_cpu(cli, monkeypatch):
    binary, audio, tmp = cli
    monkeypatch.setenv("FAKE_COLAB_NO_GPU", "1")
    with pytest.raises(RuntimeError, match="T4 当前不可用"), colab.session_scope():
        transcribe(audio, "tiny", "en", tmp, "colab-cli")
    assert "No active sessions" in sessions(binary)


@pytest.mark.parametrize("fail", [False, True])
def test_graph_worker_session_is_stopped_after_use(cli, fail):
    from langgraph.graph import END, START, StateGraph

    binary, audio, tmp = cli

    def gpu(state):
        colab.transcribe_audio(audio, tmp, "tiny", "en")
        assert "isolated-test" in sessions(binary)
        if fail:
            raise RuntimeError("graph worker failed")
        return {}

    builder = StateGraph(dict)
    builder.add_node("gpu", gpu)
    builder.add_edge(START, "gpu")
    builder.add_edge("gpu", END)
    graph = builder.compile()
    if fail:
        with pytest.raises(RuntimeError, match="graph worker failed"), colab.session_scope():
            graph.invoke({})
    else:
        with colab.session_scope():
            graph.invoke({})
    assert "No active sessions" in sessions(binary)


def test_clips_stage_stops_before_llm_and_scope_does_not_stop_twice(cli, monkeypatch):
    from langgraph.graph import END, START, StateGraph

    from video2pdf import graph as pipeline

    binary, audio, tmp = cli
    stops = []
    stop = colab.machinery._colab_stop_session

    def record_stop(reason):
        stops.append(reason)
        return stop(reason)

    monkeypatch.setattr(colab.machinery, "_colab_stop_session", record_stop)
    monkeypatch.setattr(pipeline, "_reload", lambda rid, default: default)
    monkeypatch.setattr(pipeline, "_run_stage", lambda *args: colab.transcribe_audio(audio, tmp, "tiny", "en"))

    def llm(state):
        assert "No active sessions" in sessions(binary)
        return {}

    builder = StateGraph(pipeline.RunState)
    builder.add_node("clips", pipeline._stage_node("clips"))
    builder.add_node("llm", llm)
    builder.add_edge(START, "clips")
    builder.add_edge("clips", "llm")
    builder.add_edge("llm", END)
    with colab.session_scope():
        builder.compile().invoke({"run_id": "test", "run": {}})
    assert stops == ["GPU 阶段结束"]


@pytest.mark.parametrize("fail", [False, True])
def test_standalone_calls_also_stop_on_success_and_error(cli, monkeypatch, fail):
    binary, audio, tmp = cli
    if fail:
        monkeypatch.setenv("FAKE_COLAB_FAIL_EXEC", "1")
        with pytest.raises(RuntimeError, match="remote worker failed"):
            colab.transcribe_audio(audio, tmp, "tiny", "en")
    else:
        colab.transcribe_audio(audio, tmp, "tiny", "en")
    assert "No active sessions" in sessions(binary)


def test_failed_shutdown_retries_at_scope_exit(cli, monkeypatch):
    binary, audio, tmp = cli
    stop = colab.machinery._colab_stop_session
    attempts = []

    def fail_once(reason):
        attempts.append(reason)
        return False if len(attempts) == 1 else stop(reason)

    monkeypatch.setattr(colab.machinery, "_colab_stop_session", fail_once)
    with pytest.raises(RuntimeError, match="Colab 关机失败"), colab.session_scope():
        colab.transcribe_audio(audio, tmp, "tiny", "en")
        colab.release_session()
    assert len(attempts) == 2
    assert "No active sessions" in sessions(binary)


def test_failed_shutdown_does_not_hide_original_error(cli, monkeypatch):
    _binary, audio, tmp = cli
    monkeypatch.setattr(colab.machinery, "_colab_stop_session", lambda reason: False)
    with pytest.raises(ValueError, match="original error"), colab.session_scope():
        colab.transcribe_audio(audio, tmp, "tiny", "en")
        raise ValueError("original error")


def test_stop_command_retries_timeout(cli, monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(colab.machinery.subprocess, "run", run)
    colab._configure()
    assert colab.machinery._colab_stop_session("test timeout retry")
    assert len(calls) == 2
    assert all(args[-3:] == ["stop", "-s", "isolated-test"] for args in calls)
