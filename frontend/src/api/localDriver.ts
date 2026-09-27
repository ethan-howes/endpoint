// Moves a car along a path on the client. Used wherever the backend doesn't drive the car:
// the placeholder API, standard pickups (the orchestrator confirms them without simulating),
// and the trip to the destination (no orchestrator endpoint yet).

import { pathLengthM, pointAlong } from '../lib/geo'
import type { LatLng } from './types'

export class LocalDriver {
  private startedAt = Date.now()
  private offsetM = 0
  readonly totalM: number

  /**
   * @param path       where the car drives
   * @param speedMps   on-screen speed in meters per real second
   * @param etaScale   real-world seconds per meter, for the ETA shown to the rider
   */
  constructor(readonly path: LatLng[], private readonly speedMps: number, private readonly etaScale = 1 / 11) {
    this.totalM = pathLengthM(path)
  }

  get alongM(): number {
    return Math.min(this.totalM, this.offsetM + ((Date.now() - this.startedAt) / 1000) * this.speedMps)
  }

  get remainingM(): number {
    return Math.max(0, this.totalM - this.alongM)
  }

  get arrived(): boolean {
    return this.remainingM <= 1
  }

  /** Seconds the rider is told the car still needs (in simulated, real-world time). */
  get etaS(): number {
    return Math.round(this.remainingM * this.etaScale)
  }

  snapshot(): { position: LatLng; heading: number } {
    return pointAlong(this.path, this.alongM)
  }

  /** Jump so that `remainingM` meters are left (the demo "skip to arrival"). */
  jumpToRemaining(remainingM: number) {
    this.offsetM = Math.max(0, this.totalM - remainingM)
    this.startedAt = Date.now()
  }
}
