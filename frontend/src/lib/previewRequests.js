// Shared by all viewers: rapid scrolling must not flood the page renderer.
export function createPreviewQueue(limit = 4) {
  let active = 0
  const pending = []
  function drain() {
    while (active < limit && pending.length) {
      const job = pending.shift()
      if (job.signal.aborted) continue
      active++
      Promise.resolve().then(() => {
        job.signal.throwIfAborted()
        return job.load(job.signal)
      }).then(job.resolve, job.reject).finally(() => {
        active--
        drain()
      })
    }
  }
  return (load, signal) => new Promise((resolve, reject) => {
    signal.throwIfAborted()
    const job = { load, signal, resolve, reject }
    const cancel = () => {
      const index = pending.indexOf(job)
      if (index < 0) return // Active loads own their result and blob cleanup.
      pending.splice(index, 1)
      reject(signal.reason)
    }
    signal.addEventListener('abort', cancel, { once: true })
    job.resolve = (value) => { signal.removeEventListener('abort', cancel); resolve(value) }
    job.reject = (error) => { signal.removeEventListener('abort', cancel); reject(error) }
    pending.push(job)
    drain()
  })
}

export const loadPreviewPage = createPreviewQueue()
