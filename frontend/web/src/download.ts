import { downloadBundle } from './api'

/** Download a run's Markdown bundle (note, transcript, attachments) as a zip. */
export async function saveBundle(runId: string): Promise<void> {
  const url = URL.createObjectURL(await downloadBundle(runId))
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `video2md-${runId}.zip`
  document.body.append(anchor)
  anchor.click()
  anchor.remove()
  setTimeout(() => URL.revokeObjectURL(url), 60_000)
}
