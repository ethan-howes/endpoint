import { useEffect, useState } from 'react'
import { DemoPanel } from './components/DemoPanel'
import { MapView } from './components/MapView'
import { RidePanel } from './components/RidePanel'
import { useRide } from './useRide'

const WIDE = '(min-width: 821px)'
const DEMO_PANEL_PX = 332 // demo panel width + margin, kept clear when framing the map

export function App() {
  const ride = useRide()
  const [wide, setWide] = useState(() => window.matchMedia(WIDE).matches)
  const [demoOpen, setDemoOpen] = useState(wide)

  useEffect(() => {
    const mq = window.matchMedia(WIDE)
    const onChange = () => setWide(mq.matches)
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])

  return (
    <div className="app">
      <RidePanel ride={ride} />
      <main className="map-area">
        <MapView ride={ride} insetRight={demoOpen && wide ? DEMO_PANEL_PX : 0} />
        <DemoPanel ride={ride} open={demoOpen} setOpen={setDemoOpen} />
      </main>
    </div>
  )
}
