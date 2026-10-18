"""Real subprocess/file transfer contracts; only the Google CLI boundary is fake."""
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from video2md import artsink, colab
from video2md.clips import remote_cut_clips
from video2md.frames import ocr_candidates
from video2md.transcribe import transcribe


@pytest.fixture
def cli(monkeypatch, tmp_path):
    binary = Path(__file__).parent / "fixtures/fake_colab_cli.py"
    for key, value in {
        "V2MD_COLAB_BIN": str(binary), "V2MD_COLAB_LOCK": str(tmp_path / "session.lock"),
        "V2MD_COLAB_SESSION": "isolated-test", "V2MD_REMOTE": "colab-cli",
        "V2MD_OCR_REMOTE": "colab-cli", "V2MD_CLIPS_REMOTE": "colab-cli",
        "V2MD_CLIPS_SOURCE": "local", "V2MD_COLAB_CPU_FALLBACK": "off",
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

    from video2md import graph as pipeline

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
