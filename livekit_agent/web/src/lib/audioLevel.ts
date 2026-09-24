/** Reads a live RMS amplitude (0-1) off a MediaStreamTrack via the Web Audio API. */
export class LevelAnalyser {
  private readonly ctx: AudioContext;
  private readonly analyser: AnalyserNode;
  private readonly source: MediaStreamAudioSourceNode;
  private readonly data: Uint8Array<ArrayBuffer>;
  private disposed = false;

  constructor(track: MediaStreamTrack) {
    this.ctx = new AudioContext();
    this.analyser = this.ctx.createAnalyser();
    this.analyser.fftSize = 512;
    this.analyser.smoothingTimeConstant = 0.55;
    this.source = this.ctx.createMediaStreamSource(new MediaStream([track]));
    this.source.connect(this.analyser);
    this.data = new Uint8Array(new ArrayBuffer(this.analyser.frequencyBinCount));
  }

  /** Root-mean-square of the current time-domain buffer, scaled and clamped to [0, 1]. */
  read(): number {
    this.analyser.getByteTimeDomainData(this.data);
    let sumSquares = 0;
    for (let i = 0; i < this.data.length; i++) {
      const v = (this.data[i] - 128) / 128;
      sumSquares += v * v;
    }
    const rms = Math.sqrt(sumSquares / this.data.length);
    return Math.min(1, rms * 4.5);
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.source.disconnect();
    this.analyser.disconnect();
    if (this.ctx.state !== "closed") void this.ctx.close();
  }
}
