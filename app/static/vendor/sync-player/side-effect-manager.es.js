const SOUP = "!#%()*+,-./:;=?@[]^_`{|}~ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
const SOUP_LEN = 87;
const ID_LEN = 20;
const reusedIdCarrier = [];
const genUID = () => {
  for (let i = 0; i < ID_LEN; i++) {
    reusedIdCarrier[i] = SOUP.charAt(Math.random() * SOUP_LEN);
  }
  return reusedIdCarrier.join("");
};
function invoke(fn) {
  try {
    return fn();
  } catch (e) {
    console.error(e);
  }
}
class SideEffectManager {
  constructor() {
    this.push = this.addDisposer;
    this.disposers = new Map();
  }
  addDisposer(disposer, disposerID = this.genUID()) {
    this.flush(disposerID);
    this.disposers.set(disposerID, Array.isArray(disposer) ? joinDisposers(disposer) : disposer);
    return disposerID;
  }
  add(executor, disposerID = this.genUID()) {
    const disposers = executor();
    return disposers ? this.addDisposer(disposers, disposerID) : disposerID;
  }
  addEventListener(el, type, listener, options, disposerID = this.genUID()) {
    el.addEventListener(type, listener, options);
    this.addDisposer(() => el.removeEventListener(type, listener, options), disposerID);
    return disposerID;
  }
  setTimeout(handler, timeout, disposerID = this.genUID()) {
    const ticket = window.setTimeout(() => {
      this.remove(disposerID);
      handler();
    }, timeout);
    return this.addDisposer(() => window.clearTimeout(ticket), disposerID);
  }
  setInterval(handler, timeout, disposerID = this.genUID()) {
    const ticket = window.setInterval(handler, timeout);
    return this.addDisposer(() => window.clearInterval(ticket), disposerID);
  }
  remove(disposerID) {
    const disposer = this.disposers.get(disposerID);
    this.disposers.delete(disposerID);
    return disposer;
  }
  flush(disposerID) {
    const disposer = this.remove(disposerID);
    if (disposer) {
      disposer();
    }
  }
  flushAll() {
    this.disposers.forEach(invoke);
    this.disposers.clear();
  }
  genUID() {
    let uid;
    do {
      uid = genUID();
    } while (this.disposers.has(uid));
    return uid;
  }
}
function joinDisposers(disposers) {
  return () => disposers.forEach(invoke);
}
class AsyncSideEffectManager {
  constructor() {
    this.push = this.addDisposer;
    this.finished = Promise.resolve();
    this.disposers = new Map();
    this._nextTask = new Map();
    this._isRunning = new Set();
  }
  add(executor, disposerID = this.genUID()) {
    if (this._isRunning.has(disposerID)) {
      this._nextTask.set(disposerID, () => this._add(executor, disposerID));
    } else {
      this._add(executor, disposerID);
    }
    return disposerID;
  }
  async _add(executor, disposerID) {
    this._startTask(disposerID);
    const disposer = this.remove(disposerID);
    if (disposer) {
      try {
        await disposer();
      } catch (e) {
        console.error(e);
      }
    }
    try {
      const disposers = await executor();
      if (disposers) {
        this.disposers.set(disposerID, Array.isArray(disposers) ? joinAsyncDisposers(disposers) : disposers);
      }
    } catch (e) {
      console.error(e);
    }
    this._endTask(disposerID);
    const task = this._nextTask.get(disposerID);
    if (task) {
      this._nextTask.delete(disposerID);
      task();
    }
  }
  addDisposer(disposer, disposerID = this.genUID()) {
    return this.add(() => disposer, disposerID);
  }
  remove(disposerID) {
    const disposer = this.disposers.get(disposerID);
    this.disposers.delete(disposerID);
    return disposer;
  }
  flush(disposerID) {
    if (this._isRunning.has(disposerID)) {
      this._nextTask.set(disposerID, () => this._flush(disposerID));
    } else {
      this._flush(disposerID);
    }
  }
  async _flush(disposerID) {
    const disposer = this.remove(disposerID);
    if (disposer) {
      this._startTask(disposerID);
      try {
        await disposer();
      } catch (e) {
        console.error(e);
      }
      this._endTask(disposerID);
    }
    const task = this._nextTask.get(disposerID);
    if (task) {
      this._nextTask.delete(disposerID);
      task();
    }
  }
  flushAll() {
    this.disposers.forEach((_, disposerID) => this.flush(disposerID));
  }
  genUID() {
    let uid;
    do {
      uid = genUID();
    } while (this.disposers.has(uid));
    return uid;
  }
  _startTask(disposerID) {
    this._isRunning.add(disposerID);
    if (!this._resolveFinished) {
      this.finished = new Promise((resolve) => {
        this._resolveFinished = resolve;
      });
    }
  }
  _endTask(disposerID) {
    this._isRunning.delete(disposerID);
    if (this._resolveFinished && this._isRunning.size <= 0 && this._nextTask.size <= 0) {
      this._resolveFinished();
      this._resolveFinished = void 0;
    }
  }
}
function joinAsyncDisposers(disposers) {
  return () => Promise.all(disposers.map(invoke));
}
class Disposable {
  constructor() {
    this.push = this.addDisposer;
    this.disposers = new Set();
  }
  addDisposer(disposers) {
    if (Array.isArray(disposers)) {
      disposers.forEach((disposer) => {
        this.disposers.add(disposer);
      });
    } else {
      this.disposers.add(disposers);
    }
  }
  add(executor) {
    const disposers = executor();
    if (disposers) {
      this.push(disposers);
    }
  }
  addEventListener(el, type, listener, options) {
    el.addEventListener(type, listener, options);
    const disposer = () => el.removeEventListener(type, listener, options);
    this.push(disposer);
    return disposer;
  }
  setTimeout(handler, timeout) {
    const ticket = window.setTimeout(() => {
      this.remove(disposer);
      handler();
    }, timeout);
    const disposer = () => window.clearTimeout(ticket);
    this.push(disposer);
    return disposer;
  }
  setInterval(handler, timeout) {
    const ticket = setInterval(handler, timeout);
    const disposer = () => clearInterval(ticket);
    this.push(disposer);
    return disposer;
  }
  remove(disposer) {
    this.disposers.delete(disposer);
  }
  flush(disposer) {
    this.remove(disposer);
    disposer();
  }
  flushAll() {
    this.disposers.forEach(invoke);
    this.disposers.clear();
  }
}
export { AsyncSideEffectManager, Disposable, SideEffectManager, genUID, joinAsyncDisposers, joinDisposers };
//# sourceMappingURL=side-effect-manager.es.js.map
