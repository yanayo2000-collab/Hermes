function createApprovalRunStore({ ttlMs = 5 * 60 * 1000, now = () => Date.now() } = {}) {
  const inFlight = new Map();
  const completed = new Map();

  function prune() {
    const threshold = Number(now()) - Number(ttlMs || 0);
    for (const [key, entry] of completed.entries()) {
      if (Number(entry.completedAt || 0) < threshold) {
        completed.delete(key);
      }
    }
  }

  async function run(approvalRunId, factory) {
    const key = String(approvalRunId || '').trim();
    if (!key) {
      return await factory();
    }
    prune();
    if (completed.has(key)) {
      return completed.get(key).result;
    }
    if (inFlight.has(key)) {
      return await inFlight.get(key);
    }
    const promise = Promise.resolve()
      .then(() => factory())
      .then((result) => {
        completed.set(key, { result, completedAt: Number(now()) });
        inFlight.delete(key);
        return result;
      })
      .catch((error) => {
        inFlight.delete(key);
        throw error;
      });
    inFlight.set(key, promise);
    return await promise;
  }

  return {
    run,
    _prune: prune,
    _stats() {
      return {
        in_flight: inFlight.size,
        completed: completed.size,
      };
    },
  };
}

module.exports = {
  createApprovalRunStore,
};
