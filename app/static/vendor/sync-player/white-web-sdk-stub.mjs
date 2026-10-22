/**
 * Заглушка для white-web-sdk — потрібна лише для WhiteboardPlayer у бандлі sync-player.
 * У POC/підрахунку whiteboard не використовується; достатньо констант PlayerPhase.
 */
export const PlayerPhase = {
  Playing: "playing",
  Pause: "pause",
  Stopped: "stopped",
  Ended: "ended",
};
