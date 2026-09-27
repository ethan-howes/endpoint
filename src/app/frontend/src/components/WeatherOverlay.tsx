import { useEffect, useRef } from 'react'
import type { Condition } from '../api/types'

/** Animated rain drawn on a canvas over the map. Purely visual; it doesn't block the map. */
function Rain() {
  const canvasRef = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    const ctx = canvas?.getContext('2d')
    if (!canvas || !ctx) return
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches

    let w = 0
    let h = 0
    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    const resize = () => {
      w = canvas.clientWidth
      h = canvas.clientHeight
      canvas.width = w * dpr
      canvas.height = h * dpr
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    }
    resize()
    const ro = new ResizeObserver(resize)
    ro.observe(canvas)

    const WIND = 0.22 // horizontal drift per unit of fall
    const count = Math.round((w * h) / 3600)
    const drops = Array.from({ length: count }, () => ({
      x: Math.random() * (w + 200) - 100,
      y: Math.random() * h,
      len: 10 + Math.random() * 16,
      speed: 520 + Math.random() * 420, // px per second
      alpha: 0.4 + Math.random() * 0.4,
    }))
    const splashes: { x: number; y: number; age: number }[] = []

    let raf = 0
    let last = performance.now()
    const draw = (now: number) => {
      const dt = Math.min(0.05, (now - last) / 1000)
      last = now
      ctx.clearRect(0, 0, w, h)
      ctx.lineCap = 'round'
      ctx.lineWidth = 1.5
      for (const d of drops) {
        if (!reduced) {
          d.y += d.speed * dt
          d.x += d.speed * dt * WIND
          if (d.y > h) {
            if (Math.random() < 0.25) splashes.push({ x: d.x, y: h - Math.random() * h * 0.9, age: 0 })
            d.y = -d.len
            d.x = Math.random() * (w + 200) - 100
          }
        }
        ctx.strokeStyle = `rgba(45, 80, 130, ${d.alpha})`
        ctx.beginPath()
        ctx.moveTo(d.x, d.y)
        ctx.lineTo(d.x - d.len * WIND, d.y - d.len)
        ctx.stroke()
      }
      // Tiny ripples where drops "land", so it reads as rain hitting the ground.
      for (let i = splashes.length - 1; i >= 0; i--) {
        const s = splashes[i]
        s.age += dt
        if (s.age > 0.45) { splashes.splice(i, 1); continue }
        const t = s.age / 0.45
        ctx.strokeStyle = `rgba(70, 105, 150, ${0.35 * (1 - t)})`
        ctx.beginPath()
        ctx.ellipse(s.x, s.y, 2 + t * 7, 1 + t * 2.5, 0, 0, Math.PI * 2)
        ctx.stroke()
      }
      if (!reduced) raf = requestAnimationFrame(draw)
    }
    raf = requestAnimationFrame(draw)
    return () => { cancelAnimationFrame(raf); ro.disconnect() }
  }, [])

  return <canvas ref={canvasRef} className="wx-canvas" />
}

/**
 * Weather layer over the map: rain animation when it's raining at pickup, a warm glow when the
 * sun is strong. Sits above the tiles and below the panels; clicks pass straight through.
 */
export function WeatherOverlay({ condition }: { condition: Condition | null }) {
  if (condition === 'rain') {
    return (
      <div className="wx wx--rain" aria-hidden>
        <Rain />
      </div>
    )
  }
  if (condition === 'sun') return <div className="wx wx--sun" aria-hidden />
  return null
}
