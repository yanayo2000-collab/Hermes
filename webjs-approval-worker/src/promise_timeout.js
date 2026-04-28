function withTimeout(promise, { timeoutMs, label = 'operation' } = {}) {
  const normalizedTimeout = Math.max(1, Number(timeoutMs || 0));
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(new Error(`${label} timed out after ${normalizedTimeout}ms`));
    }, normalizedTimeout);
    Promise.resolve(promise)
      .then((value) => {
        clearTimeout(timer);
        resolve(value);
      })
      .catch((error) => {
        clearTimeout(timer);
        reject(error);
      });
  });
}

module.exports = {
  withTimeout,
};
