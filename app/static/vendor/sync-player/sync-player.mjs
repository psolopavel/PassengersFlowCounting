import EventEmitter from "eventemitter3";
import { SideEffectManager } from "side-effect-manager";
import { PlayerPhase } from "white-web-sdk";
var SyncPlayerStatus = /* @__PURE__ */ ((SyncPlayerStatus2) => {
  SyncPlayerStatus2["Ready"] = "Ready";
  SyncPlayerStatus2["Pause"] = "Pause";
  SyncPlayerStatus2["Buffering"] = "Buffering";
  SyncPlayerStatus2["Playing"] = "Playing";
  SyncPlayerStatus2["Ended"] = "Ended";
  return SyncPlayerStatus2;
})(SyncPlayerStatus || {});
function normalizePlaybackRate(rate) {
  return Math.floor(rate * 1e3) / 1e3;
}
function isPlaybackRateEqual(rate1, rate2) {
  return Math.floor(rate1 * 1e3) === Math.floor(rate2 * 1e3);
}
class AtomPlayer extends EventEmitter {
  constructor(config) {
    super();
    this._offset = 0;
    this._visible = true;
    this.ignoreSetStatus = false;
    this._sideEffect = new SideEffectManager();
    this._status = SyncPlayerStatus.Ready;
    this._currentTime = 0;
    this._duration = 0;
    this._playbackRate = 1;
    this._initStatus = AtomPlayerInitStatus.Idle;
    this.name = config == null ? void 0 : config.name;
    this.loadInit = this.init();
  }
  get offset() {
    return this._offset;
  }
  get visible() {
    return this._visible;
  }
  set visible(value) {
    if (this._visible !== value) {
      this._visible = value;
      this.emit("visibilitychange");
    }
  }
  get isReady() {
    return this._initStatus === AtomPlayerInitStatus.Ready;
  }
  get isPlaying() {
    return this._status === SyncPlayerStatus.Playing || this._status === SyncPlayerStatus.Buffering;
  }
  get status() {
    return this._status;
  }
  set status(value) {
    if (!this.ignoreSetStatus && this._status !== value) {
      this._status = value;
      this.emit("status");
    }
  }
  get currentTime() {
    return this._currentTime;
  }
  set currentTime(ms) {
    ms = Math.floor(ms);
    if (this._currentTime !== ms) {
      this._currentTime = ms;
      this.emit("timeupdate");
    }
  }
  get duration() {
    return this._duration;
  }
  set duration(ms) {
    ms = Math.floor(ms);
    if (this._duration !== ms) {
      this._duration = ms;
      this.emit("durationchange");
    }
  }
  get playbackRate() {
    return this._playbackRate;
  }
  set playbackRate(value) {
    const rate = normalizePlaybackRate(value);
    if (!isPlaybackRateEqual(this._playbackRate, rate)) {
      this.setPlaybackRateImpl(rate);
      this._playbackRate = rate;
      this.emit("ratechange");
    }
  }
  setPlaybackRate(value) {
    this.playbackRate = value;
  }
  async play() {
    if (!this.isReady) {
      this.status = SyncPlayerStatus.Buffering;
      await this.loadInit;
    }
    if (this._status !== SyncPlayerStatus.Playing) {
      try {
        await this.playImpl();
      } catch (e) {
        if (this._status !== SyncPlayerStatus.Ready && this._status !== SyncPlayerStatus.Pause) {
          throw e;
        }
      }
    }
  }
  async pause() {
    if (!this.isReady) {
      await this.loadInit;
    }
    if (this._status !== SyncPlayerStatus.Pause && this._status !== SyncPlayerStatus.Ended) {
      this.status = SyncPlayerStatus.Pause;
      await this.pauseImpl();
    }
  }
  async stop() {
    if (!this.isReady) {
      await this.loadInit;
    }
    if (this._status !== SyncPlayerStatus.Ended) {
      this.status = SyncPlayerStatus.Ended;
      this.ignoreSetStatus = true;
      await this.readyImpl(true);
      await this.stopImpl();
      this.ignoreSetStatus = false;
    }
  }
  async seek(ms) {
    if (!this.isReady) {
      await this.loadInit;
    }
    ms = Math.floor(ms);
    if (ms === this._currentTime) {
      return;
    }
    if (ms >= this.duration) {
      await this.stop();
      return;
    }
    const lastStatus = this._status;
    await this.seekImpl(ms);
    switch (lastStatus) {
      case SyncPlayerStatus.Ready:
      case SyncPlayerStatus.Ended: {
        await this.ready();
        break;
      }
      case SyncPlayerStatus.Pause: {
        await this.pause();
        break;
      }
      case SyncPlayerStatus.Buffering:
      case SyncPlayerStatus.Playing: {
        await this.play();
        break;
      }
    }
  }
  async ready(silently) {
    if (this._status !== SyncPlayerStatus.Ready) {
      if (silently !== void 0) {
        this.ignoreSetStatus = silently;
      }
      this.status = SyncPlayerStatus.Ready;
      await this.readyImpl(silently);
      if (silently !== void 0) {
        this.ignoreSetStatus = false;
      }
    }
  }
  destroy() {
    this._sideEffect.flushAll();
  }
  initImpl() {
    return Promise.resolve();
  }
  async init() {
    switch (this._initStatus) {
      case AtomPlayerInitStatus.Ready: {
        return;
      }
      case AtomPlayerInitStatus.Initializing: {
        return new Promise((resolve) => this.once("ready", resolve));
      }
      default: {
        this.ignoreSetStatus = true;
        this._initStatus = AtomPlayerInitStatus.Initializing;
        await new Promise((r) => setTimeout(r));
        await this.initImpl();
        this._initStatus = AtomPlayerInitStatus.Ready;
        this.emit("ready");
        this.ignoreSetStatus = false;
      }
    }
  }
}
var AtomPlayerInitStatus = /* @__PURE__ */ ((AtomPlayerInitStatus2) => {
  AtomPlayerInitStatus2[AtomPlayerInitStatus2["Idle"] = 0] = "Idle";
  AtomPlayerInitStatus2[AtomPlayerInitStatus2["Initializing"] = 1] = "Initializing";
  AtomPlayerInitStatus2[AtomPlayerInitStatus2["Ready"] = 2] = "Ready";
  return AtomPlayerInitStatus2;
})(AtomPlayerInitStatus || {});
class ClusterPlayer extends AtomPlayer {
  constructor(config) {
    super({
      name: config.name || `{${config.rowPlayer.name || "unknown"}-${config.colPlayer.name || "unknown"}}`
    });
    this.rowPlayer = config.rowPlayer;
    this.colPlayer = config.colPlayer;
    this.longerPlayer = this.rowPlayer.duration >= this.colPlayer.duration ? this.rowPlayer : this.colPlayer;
    this.duration = this.longerPlayer.duration;
    this.playbackRate = this.longerPlayer.playbackRate;
    const addAtomListener = (event, listener) => {
      this._sideEffect.add(() => {
        const handler = () => {
          listener(this.rowPlayer, this.colPlayer);
        };
        this.rowPlayer.on(event, handler);
        return () => {
          this.rowPlayer.off(event, handler);
        };
      });
      this._sideEffect.add(() => {
        const handler = () => {
          listener(this.colPlayer, this.rowPlayer);
        };
        this.colPlayer.on(event, handler);
        return () => {
          this.colPlayer.off(event, handler);
        };
      });
    };
    addAtomListener("status", (emitter, receptor) => {
      var _a;
      if (!this.ignoreSetStatus) {
        this.syncSubPlayer(emitter, receptor);
        this.updateStatus(emitter, receptor);
        if (this.rowPlayer.status === SyncPlayerStatus.Playing && this.colPlayer.status === SyncPlayerStatus.Playing) {
          this.startFrameDropCheck();
        } else {
          (_a = this.stopFrameDropCheck) == null ? void 0 : _a.call(this);
        }
      }
    });
    addAtomListener("timeupdate", (emitter) => {
      if (this.longerPlayer === emitter && emitter.status !== SyncPlayerStatus.Ended) {
        this.currentTime = emitter.currentTime;
      }
    });
    addAtomListener("durationchange", (emitter, receptor) => {
      if (emitter.duration >= this.longerPlayer.duration) {
        if (emitter !== this.longerPlayer) {
          this.longerPlayer = emitter;
        }
      } else {
        this.longerPlayer = receptor;
      }
      this.duration = this.longerPlayer.duration;
    });
    addAtomListener("ratechange", (emitter) => {
      if (!isPlaybackRateEqual(emitter.playbackRate, this.playbackRate)) {
        this.playbackRate = emitter.playbackRate;
      }
    });
  }
  destroy() {
    var _a;
    super.destroy();
    (_a = this.stopFrameDropCheck) == null ? void 0 : _a.call(this);
    this.rowPlayer.destroy();
    this.colPlayer.destroy();
  }
  get isReady() {
    return this.rowPlayer.isReady && this.colPlayer.isReady;
  }
  async ready(silently) {
    await this.readyImpl(silently);
  }
  async play() {
    if (this.status !== SyncPlayerStatus.Ended) {
      await this.playImpl();
    }
  }
  async pause() {
    await this.pauseImpl();
  }
  async stop() {
    await this.stopImpl();
  }
  async seek(ms) {
    await this.seekImpl(ms);
  }
  async readyImpl(silently) {
    await this.invokeSubPlayers((player) => player.ready(silently));
  }
  async playImpl() {
    await this.invokeSubPlayers((player) => player.play());
  }
  async pauseImpl() {
    await this.invokeSubPlayers((player) => player.pause());
  }
  async stopImpl() {
    await this.invokeSubPlayers((player) => player.stop());
  }
  async seekImpl(ms) {
    await Promise.all([this.rowPlayer.seek(ms), this.colPlayer.seek(ms)]);
  }
  setPlaybackRateImpl(value) {
    this.rowPlayer.setPlaybackRate(value);
    this.colPlayer.setPlaybackRate(value);
  }
  updateStatus(emitter, receptor) {
    switch (emitter.status) {
      case SyncPlayerStatus.Ready: {
        switch (receptor.status) {
          case SyncPlayerStatus.Ready:
          case SyncPlayerStatus.Ended: {
            this.status = SyncPlayerStatus.Ready;
            break;
          }
        }
        break;
      }
      case SyncPlayerStatus.Pause: {
        if (receptor.status !== SyncPlayerStatus.Playing) {
          this.status = SyncPlayerStatus.Pause;
        }
        break;
      }
      case SyncPlayerStatus.Buffering: {
        if (receptor.status !== SyncPlayerStatus.Pause) {
          this.status = SyncPlayerStatus.Buffering;
        }
        break;
      }
      case SyncPlayerStatus.Playing: {
        switch (receptor.status) {
          case SyncPlayerStatus.Playing:
          case SyncPlayerStatus.Ended: {
            this.status = SyncPlayerStatus.Playing;
            break;
          }
        }
        break;
      }
      case SyncPlayerStatus.Ended: {
        this.status = receptor.status;
        break;
      }
    }
  }
  async syncSubPlayer(emitter, receptor) {
    switch (emitter.status) {
      case SyncPlayerStatus.Pause: {
        if (receptor.isPlaying) {
          await receptor.pause();
        }
        break;
      }
      case SyncPlayerStatus.Buffering: {
        if (receptor.status === SyncPlayerStatus.Playing) {
          await receptor.ready();
        }
        break;
      }
      case SyncPlayerStatus.Playing: {
        if (receptor.status === SyncPlayerStatus.Buffering) {
          await emitter.ready();
        } else if (receptor.status === SyncPlayerStatus.Ready && (receptor.duration <= 0 || emitter.currentTime < receptor.duration)) {
          await receptor.play();
        }
        break;
      }
    }
  }
  startFrameDropCheck() {
    if (this.stopFrameDropCheck) {
      return;
    }
    let frameDropCount = 0;
    const ticket = setInterval(async () => {
      if (this.status !== SyncPlayerStatus.Playing) {
        frameDropCount = 0;
      } else {
        const diff = this.rowPlayer.currentTime - this.colPlayer.currentTime;
        frameDropCount = Math.abs(diff) > 1e3 ? frameDropCount + 1 : 0;
        if (frameDropCount >= 2) {
          if (diff < 0) {
            await this.rowPlayer.seek(this.colPlayer.currentTime);
          } else {
            await this.colPlayer.seek(this.rowPlayer.currentTime);
          }
          frameDropCount = 0;
        }
      }
    }, 2e3);
    this.stopFrameDropCheck = () => {
      this.stopFrameDropCheck = void 0;
      clearInterval(ticket);
    };
  }
  async invokeSubPlayers(action) {
    await Promise.all(
      [this.rowPlayer, this.colPlayer].filter((player) => player.status !== SyncPlayerStatus.Ended).map(action)
    );
  }
}
function addEventListener(target, type, listener, options) {
  target.addEventListener(type, listener, options);
  return () => target.removeEventListener(type, listener, options);
}
const listen = addEventListener;
class NativeVideoPlayer extends AtomPlayer {
  constructor(config) {
    super(config);
    this.video = config.video;
    this.video.controls = false;
    const addVideoListener = (type, listener) => {
      this._sideEffect.push(listen(this.video, type, listener));
    };
    [
      "waiting",
      "canplay",
      "pause",
      "suspend",
      "playing",
      "play",
      "seeking",
      "seeked",
      "stalled",
      "canplaythrough"
    ].forEach((eventName) => {
      addVideoListener(eventName, (e) => {
        if (this.status === SyncPlayerStatus.Ended) {
          return;
        }
        const eventType = e.type;
        if (this.video.paused || eventType === "seeking") {
          if (this.status !== SyncPlayerStatus.Pause && this.status !== SyncPlayerStatus.Ready) {
            if (eventType === "pause") {
              this.status = SyncPlayerStatus.Ready;
            } else {
              this.status = SyncPlayerStatus.Buffering;
            }
          }
        } else {
          this.status = SyncPlayerStatus.Playing;
        }
      });
    });
    addVideoListener("error", () => {
      if (this.status === SyncPlayerStatus.Playing && this.video.paused) {
        this.status = SyncPlayerStatus.Buffering;
      }
    });
    addVideoListener("ended", () => {
      this.status = SyncPlayerStatus.Ended;
    });
    const setCurrentTime = () => {
      this.currentTime = this.video.currentTime * 1e3;
    };
    setCurrentTime();
    addVideoListener("timeupdate", setCurrentTime);
    const setDuration = () => {
      this.duration = this.video.duration * 1e3;
    };
    setDuration();
    addVideoListener("durationchange", setDuration);
  }
  async readyImpl() {
    this.video.pause();
  }
  async playImpl() {
    try {
      await this.video.play();
    } catch {
    }
  }
  async pauseImpl() {
    this.video.pause();
  }
  async stopImpl() {
    this.video.currentTime = this.duration / 1e3 - 0.5;
  }
  async seekImpl(ms) {
    this.video.currentTime = ms / 1e3;
  }
  setPlaybackRateImpl(value) {
    this.video.playbackRate = value;
  }
}
class ChromeAtomPlayer extends AtomPlayer {
  constructor(config) {
    super(config);
    this.video = config.video;
    this.video.controls(false);
    const addVideoListener = (type, listener) => {
      this.video.on(type, listener);
      this._sideEffect.addDisposer(() => this.video.off(type, listener));
    };
    addVideoListener(
      [
        "waiting",
        "canplay",
        "pause",
        "suspend",
        "playing",
        "play",
        "seeking",
        "seeked",
        "stalled",
        "canplaythrough"
      ],
      (e) => {
        if (this.status === SyncPlayerStatus.Ended) {
          return;
        }
        const eventType = e == null ? void 0 : e.type;
        if (this.video.paused() || eventType === "seeking") {
          if (this.status !== SyncPlayerStatus.Pause && this.status !== SyncPlayerStatus.Ready) {
            if (eventType === "pause") {
              this.status = SyncPlayerStatus.Ready;
            } else {
              this.status = SyncPlayerStatus.Buffering;
            }
          }
        } else {
          this.status = SyncPlayerStatus.Playing;
        }
      }
    );
    addVideoListener("error", () => {
      if (this.status === SyncPlayerStatus.Playing && this.video.paused()) {
        this.status = SyncPlayerStatus.Buffering;
      }
    });
    addVideoListener("ended", () => {
      this.status = SyncPlayerStatus.Ended;
    });
    const setCurrentTime = () => {
      this.currentTime = (this.video.currentTime() || 0) * 1e3;
    };
    setCurrentTime();
    addVideoListener("timeupdate", setCurrentTime);
    const setDuration = () => {
      this.duration = (this.video.duration() || 0) * 1e3;
    };
    setDuration();
    addVideoListener("durationchange", setDuration);
  }
  async readyImpl() {
    try {
      this.video.pause();
    } catch {
    }
  }
  async playImpl() {
    try {
      await this.video.play();
    } catch {
    }
  }
  async pauseImpl() {
    try {
      this.video.pause();
    } catch {
    }
  }
  async stopImpl() {
    try {
      this.video.currentTime(this.duration / 1e3 - 0.5);
    } catch {
    }
  }
  async seekImpl(ms) {
    try {
      this.video.currentTime(ms / 1e3);
    } catch {
    }
  }
  setPlaybackRateImpl(value) {
    try {
      this.video.playbackRate(value);
    } catch {
    }
  }
}
class SafariAtomPlayer extends AtomPlayer {
  constructor(config) {
    super(config);
    this.checkCurrentTime = () => {
      if (Number.isNaN(this.checkTimeInterval)) {
        this.checkTimeInterval = window.setInterval(() => {
          if (this.lastCurrentTime === this.currentTime) {
            if (this.status === SyncPlayerStatus.Playing) {
              this.status = SyncPlayerStatus.Buffering;
            }
          } else {
            this.lastCurrentTime = this.currentTime;
          }
        }, 500);
      }
    };
    this.cleanCheckCurrentTime = () => {
      if (!Number.isNaN(this.checkTimeInterval)) {
        window.clearTimeout(this.checkTimeInterval);
        this.checkTimeInterval = NaN;
      }
    };
    this.checkTimeInterval = NaN;
    this.video = config.video;
    this.video.controls(false);
    const addVideoListener = (type, listener) => {
      this.video.on(type, listener);
      this._sideEffect.addDisposer(() => this.video.off(type, listener));
    };
    addVideoListener(
      [
        "waiting",
        "canplay",
        "pause",
        "suspend",
        "playing",
        "play",
        "seeking",
        "seeked",
        "stalled",
        "canplaythrough"
      ],
      (e) => {
        if (this.status === SyncPlayerStatus.Ended) {
          return;
        }
        const eventType = e == null ? void 0 : e.type;
        if (this.video.paused() || eventType === "seeking") {
          if (this.status !== SyncPlayerStatus.Pause && this.status !== SyncPlayerStatus.Ready) {
            if (eventType === "pause") {
              this.status = SyncPlayerStatus.Ready;
            } else {
              this.status = SyncPlayerStatus.Buffering;
            }
          }
        } else {
          this.checkCurrentTime();
          this.status = SyncPlayerStatus.Playing;
        }
      }
    );
    addVideoListener("error", () => {
      if (this.status === SyncPlayerStatus.Playing && this.video.paused()) {
        this.status = SyncPlayerStatus.Buffering;
      }
    });
    addVideoListener("ended", () => {
      this.status = SyncPlayerStatus.Ended;
    });
    const setCurrentTime = () => {
      this.currentTime = (this.video.currentTime() || 0) * 1e3;
    };
    setCurrentTime();
    addVideoListener("timeupdate", setCurrentTime);
    const setDuration = () => {
      this.duration = (this.video.duration() || 0) * 1e3;
    };
    setDuration();
    addVideoListener("durationchange", setDuration);
    this.lastCurrentTime = this.currentTime;
    this._sideEffect.add(() => {
      const handler = () => {
        if (this.status === SyncPlayerStatus.Playing) {
          this.checkCurrentTime();
        } else {
          this.cleanCheckCurrentTime();
        }
      };
      this.on("status", handler);
      return () => {
        this.cleanCheckCurrentTime();
        this.off("status", handler);
      };
    });
  }
  async readyImpl() {
    this.video.pause();
  }
  async playImpl() {
    await this.video.play();
  }
  async pauseImpl() {
    this.video.pause();
  }
  async stopImpl() {
    this.video.currentTime(this.duration / 1e3 - 0.5);
  }
  async seekImpl(ms) {
    this.video.currentTime(ms / 1e3);
  }
  setPlaybackRateImpl(value) {
    this.video.playbackRate(value);
  }
}
const VideoPlayer = function VideoPlayer2(config) {
  return /apple/i.test(navigator.vendor) ? new SafariAtomPlayer(config) : new ChromeAtomPlayer(config);
};
class WhiteboardPlayer extends AtomPlayer {
  constructor(config) {
    super(config);
    this.player = config.player;
    this._sideEffect.add(() => {
      const handler = (phase) => {
        if (this.status === SyncPlayerStatus.Ended) {
          return;
        }
        switch (phase) {
          case PlayerPhase.Ended:
          case PlayerPhase.Stopped: {
            this.status = SyncPlayerStatus.Ended;
            break;
          }
          case PlayerPhase.Playing: {
            this.status = SyncPlayerStatus.Playing;
            break;
          }
          default: {
            if (this.status !== SyncPlayerStatus.Pause && this.status !== SyncPlayerStatus.Ready) {
              this.status = SyncPlayerStatus.Buffering;
            }
            break;
          }
        }
      };
      this.player.callbacks.on("onPhaseChanged", handler);
      return () => this.player.callbacks.off("onPhaseChanged", handler);
    });
    this.duration = this.player.timeDuration || 0;
    this.playbackRate = this.player.playbackSpeed;
    this._sideEffect.add(() => {
      const handler = () => {
        this.duration = this.player.timeDuration || 0;
        this.playbackRate = this.player.playbackSpeed;
      };
      this.player.callbacks.on("onLoadFirstFrame", handler);
      return () => this.player.callbacks.off("onLoadFirstFrame", handler);
    });
    this._sideEffect.add(() => {
      const handler = (currentTime) => {
        this.currentTime = currentTime;
      };
      this.player.callbacks.on("onProgressTimeChanged", handler);
      return () => this.player.callbacks.off("onProgressTimeChanged", handler);
    });
  }
  async initImpl() {
    const p = new Promise((resolve) => {
      const { player } = this;
      const disposerID = this._sideEffect.add(() => {
        const handler = (phase) => {
          if (phase === PlayerPhase.Playing) {
            try {
              player.pause();
            } catch {
            }
          }
          if (phase === PlayerPhase.Pause) {
            this._sideEffect.flush(disposerID);
            return resolve();
          }
        };
        player.callbacks.on("onPhaseChanged", handler);
        return () => player.callbacks.off("onPhaseChanged", handler);
      });
    });
    this._safeSeek(0);
    await p;
  }
  async readyImpl() {
    this._safePause();
  }
  async playImpl() {
    const p = new Promise((resolve) => {
      const { player } = this;
      const disposerID = this._sideEffect.add(() => {
        const handler = (phase) => {
          if (phase === PlayerPhase.Playing) {
            this._sideEffect.flush(disposerID);
            resolve();
          }
        };
        player.callbacks.on("onPhaseChanged", handler);
        return () => player.callbacks.off("onPhaseChanged", handler);
      });
    });
    this._safePlay();
    await p;
  }
  async pauseImpl() {
    this._safePause();
  }
  async stopImpl() {
    this._safeSeek(this.duration);
  }
  async seekImpl(ms) {
    this._safeSeek(ms);
  }
  setPlaybackRateImpl(value) {
    try {
      this.player.playbackSpeed = value;
    } catch {
    }
  }
  _safeSeek(ms) {
    try {
      this.player.seekToProgressTime(ms);
    } catch {
    }
  }
  _safePause() {
    try {
      this.player.pause();
    } catch {
    }
  }
  _safePlay() {
    try {
      this.player.play();
    } catch {
    }
  }
}
class TickPlayer extends AtomPlayer {
  constructor({ duration, ...config }) {
    super(config);
    this.duration = duration;
    let playRafTicket = NaN;
    this._startTimer = (startTime, playbackRate) => {
      window.cancelAnimationFrame(playRafTicket);
      const startTimestamp = Date.now();
      const playRaf = () => {
        this.currentTime = (Date.now() - startTimestamp) * playbackRate + startTime;
        if (this.currentTime >= this.duration) {
          this.stop();
        } else {
          playRafTicket = window.requestAnimationFrame(playRaf);
        }
      };
      playRaf();
    };
    this._stopTimer = () => {
      window.cancelAnimationFrame(playRafTicket);
      playRafTicket = NaN;
    };
    this._resetTimer = (startTime, playbackRate) => {
      if (playRafTicket) {
        this._startTimer(startTime, playbackRate);
      } else {
        this.currentTime = startTime;
      }
    };
    this._sideEffect.addDisposer(this._stopTimer);
  }
  async readyImpl() {
    this._stopTimer();
  }
  async playImpl() {
    this._startTimer(this.currentTime, this.playbackRate);
    this.status = SyncPlayerStatus.Playing;
  }
  async pauseImpl() {
    this._stopTimer();
  }
  async stopImpl() {
    this._stopTimer();
    this.currentTime = this.duration;
  }
  async seekImpl(ms) {
    this._resetTimer(ms, this.playbackRate);
  }
  setPlaybackRateImpl(playbackRate) {
    this._resetTimer(this.currentTime, playbackRate);
  }
}
class OffsetPlayer extends AtomPlayer {
  constructor({ offset, player, ...config }) {
    super({ ...config, name: config.name || `Offset<${player.name}>` });
    this.player = player;
    this._offset = offset || 0;
    this.timer = new TickPlayer({
      name: `Tick<${this.name}>`,
      duration: this._offset
    });
    this.playbackRate = this.player.playbackRate;
    this._currentPlayer = this._offset > 0 ? this.timer : this.player;
    this.status = this.currentPlayer.status;
    this.duration = this.timer.duration + this.player.duration;
    if (this.currentPlayer === this.timer) {
      this.currentTime = this.timer.currentTime;
      this._visible = false;
    } else {
      this.currentTime = this.player.currentTime + this.offset;
    }
    this.playbackRate = this.currentPlayer.playbackRate;
    const syncAtomProps = (player2, event, listener) => {
      player2.on(event, listener);
      this._sideEffect.addDisposer(() => {
        player2.off(event, listener);
      });
    };
    syncAtomProps(this.player, "status", () => {
      if (this.currentPlayer !== this.player) {
        return;
      }
      if (this.status === SyncPlayerStatus.Ended) {
        return;
      }
      switch (this.player.status) {
        case SyncPlayerStatus.Playing: {
          this.status = SyncPlayerStatus.Playing;
          break;
        }
        case SyncPlayerStatus.Buffering: {
          this.status = SyncPlayerStatus.Buffering;
          break;
        }
        case SyncPlayerStatus.Ready: {
          this.status = SyncPlayerStatus.Ready;
          break;
        }
      }
    });
    syncAtomProps(this.player, "timeupdate", () => {
      if (this.currentPlayer === this.player) {
        this.currentTime = this.player.currentTime + this.offset;
      }
    });
    syncAtomProps(this.player, "durationchange", () => {
      this.duration = this.timer.duration + this.player.duration;
    });
    syncAtomProps(this.player, "ratechange", () => {
      if (this.currentPlayer === this.player) {
        this.playbackRate = this.player.playbackRate;
      }
    });
    syncAtomProps(this.timer, "status", async () => {
      if (this.currentPlayer !== this.timer) {
        return;
      }
      if (this.timer.status === SyncPlayerStatus.Ended) {
        this.currentPlayer = this.player;
        if (this.status === SyncPlayerStatus.Playing) {
          await this.player.seek(0);
          await this.player.play();
          return;
        }
      }
      if (this.status === SyncPlayerStatus.Ended) {
        return;
      }
      switch (this.timer.status) {
        case SyncPlayerStatus.Playing: {
          this.status = SyncPlayerStatus.Playing;
          break;
        }
        case SyncPlayerStatus.Ready: {
          this.status = SyncPlayerStatus.Ready;
          break;
        }
      }
    });
    syncAtomProps(this.timer, "timeupdate", () => {
      if (this.currentPlayer === this.timer) {
        this.currentTime = this.timer.currentTime;
      }
    });
    syncAtomProps(this.timer, "durationchange", () => {
      this.duration = this.timer.duration + this.player.duration;
    });
    syncAtomProps(this.timer, "ratechange", () => {
      if (this.currentPlayer === this.timer) {
        this.playbackRate = this.timer.playbackRate;
      }
    });
  }
  get currentPlayer() {
    return this._currentPlayer;
  }
  set currentPlayer(player) {
    if (this._currentPlayer !== player) {
      this._currentPlayer = player;
      this.visible = player !== this.timer;
    }
  }
  async readyImpl(silently) {
    await Promise.all([
      this.player.ready(silently),
      this.timer.ready(silently)
    ]);
  }
  playImpl() {
    return this.currentPlayer.play();
  }
  async pauseImpl() {
    await this.timer.pause();
    await this.player.pause();
  }
  async stopImpl() {
    await this.timer.stop();
    await this.player.stop();
  }
  async seekImpl(ms) {
    if (ms <= this.offset) {
      this.currentPlayer = this.timer;
      if (this.player.status === SyncPlayerStatus.Playing) {
        await this.player.ready(true);
      }
      if (this.player.currentTime !== 0) {
        await this.player.seek(0);
      }
      await this.timer.seek(ms);
    } else {
      this.currentPlayer = this.player;
      if (this.timer.currentTime !== 0) {
        await this.timer.stop();
      }
      await this.player.seek(ms - this.offset);
    }
  }
  setPlaybackRateImpl(value) {
    this.player.setPlaybackRate(value);
    this.timer.setPlaybackRate(value);
  }
}
class SelectionPlayer extends AtomPlayer {
  constructor({
    player,
    selectionList,
    ...config
  }) {
    super({ ...config, name: config.name || `Selection<${player.name}>` });
    this.player = player;
    const selectionItems = sanitizeSelectionList(selectionList);
    this.selectionItems = this.sliceSelectionItems(selectionItems);
    this.status = this.player.status;
    this.playbackRate = this.player.playbackRate;
    this.duration = this.calcDuration();
    this.currentTime = this.syncCurrentTime();
    const syncAtomProps = (player2, event, listener) => {
      player2.on(event, listener);
      this._sideEffect.addDisposer(() => {
        player2.off(event, listener);
      });
    };
    syncAtomProps(this.player, "status", () => {
      this.status = this.player.status;
    });
    syncAtomProps(this.player, "ratechange", () => {
      this.playbackRate = this.player.playbackRate;
    });
    syncAtomProps(this.player, "timeupdate", () => {
      this.currentTime = this.syncCurrentTime();
    });
    syncAtomProps(this.player, "durationchange", () => {
      this.selectionItems = this.sliceSelectionItems(selectionItems);
      this.duration = this.calcDuration();
      this.currentTime = this.syncCurrentTime();
    });
  }
  readyImpl(silently) {
    return this.player.ready(silently);
  }
  playImpl() {
    return this.player.play();
  }
  pauseImpl() {
    return this.player.pause();
  }
  async stopImpl() {
    await this.player.stop();
    const lastItem = this.selectionItems[this.selectionItems.length - 1];
    if (lastItem) {
      await this.player.seek(lastItem.start + lastItem.duration);
    }
  }
  setPlaybackRateImpl(value) {
    return this.player.setPlaybackRate(value);
  }
  async seekImpl(ms) {
    if (this.selectionItems.length <= 0) {
      return this.player.seek(ms);
    }
    if (ms <= 0) {
      return this.player.seek(0);
    }
    if (ms >= this.duration) {
      return this.player.seek(this.player.duration);
    }
    for (let i = this.selectionItems.length - 1; i >= 0; i--) {
      const item = this.selectionItems[i];
      if (ms >= item.rStart) {
        return this.player.seek(ms - item.rStart + item.start);
      }
    }
  }
  syncCurrentTime() {
    const currentTime = this.player.currentTime;
    if (this.selectionItems.length <= 0) {
      return currentTime;
    }
    for (let i = 0; i < this.selectionItems.length; i++) {
      const item = this.selectionItems[i];
      if (currentTime <= item.start + item.duration) {
        if (currentTime < item.start) {
          this.player.seek(item.start);
          return item.rStart;
        }
        return currentTime - item.start + item.rStart;
      }
    }
    this.stop();
    return this.duration;
  }
  calcDuration() {
    if (this.player.duration <= 0) {
      return 0;
    }
    const lastItem = this.selectionItems[this.selectionItems.length - 1];
    return lastItem ? lastItem.rStart + lastItem.duration : this.player.duration;
  }
  sliceSelectionItems(selectionItems) {
    if (this.player.duration <= 0) {
      return [];
    }
    const result = [];
    for (let i = 0; i < selectionItems.length; i++) {
      const item = selectionItems[i];
      if (item.rStart + item.duration <= this.player.duration) {
        result.push(item);
      } else {
        result.push({ ...item, duration: this.player.duration - item.rStart });
        break;
      }
    }
    return result;
  }
}
function sanitizeSelectionList(selectionList) {
  selectionList = selectionList.sort((a, b) => a.start - b.start);
  const result = [];
  let lastItem = null;
  for (let i = 0; i < selectionList.length; i++) {
    const item = selectionList[i];
    if (lastItem && item.start <= lastItem.start + lastItem.duration) {
      lastItem.duration = Math.max(
        lastItem.start + lastItem.duration,
        item.start + item.duration
      ) - lastItem.start;
    } else {
      lastItem = {
        start: item.start,
        duration: item.duration,
        rStart: lastItem ? lastItem.rStart + lastItem.duration : 0
      };
      result.push(lastItem);
    }
  }
  return result;
}
const SyncPlayer = function SyncPlayer2(config) {
  return config.players.reduce(
    (combinedPlayer, player) => new ClusterPlayer({ rowPlayer: combinedPlayer, colPlayer: player })
  );
};
export {
  AtomPlayer,
  NativeVideoPlayer,
  OffsetPlayer,
  SelectionPlayer,
  SyncPlayer,
  SyncPlayerStatus,
  VideoPlayer,
  WhiteboardPlayer
};
//# sourceMappingURL=sync-player.mjs.map