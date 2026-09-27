import type { SVGProps } from 'react'

type P = SVGProps<SVGSVGElement>
const base = { width: 20, height: 20, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 2, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const, 'aria-hidden': true }

export const IconSearch = (p: P) => <svg {...base} {...p}><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
export const IconBack = (p: P) => <svg {...base} {...p}><path d="M15 18l-6-6 6-6" /></svg>
export const IconClose = (p: P) => <svg {...base} {...p}><path d="M18 6 6 18M6 6l12 12" /></svg>
export const IconHome = (p: P) => <svg {...base} {...p}><path d="M3 11 12 4l9 7" /><path d="M5 10v10h14V10" /></svg>
export const IconWork = (p: P) => <svg {...base} {...p}><rect x="3" y="7" width="18" height="13" rx="2" /><path d="M9 7V5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2" /></svg>
export const IconPin = (p: P) => <svg {...base} {...p}><path d="M12 21s-7-6.2-7-12a7 7 0 0 1 14 0c0 5.8-7 12-7 12Z" /><circle cx="12" cy="9" r="2.5" /></svg>
export const IconClock = (p: P) => <svg {...base} {...p}><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></svg>
export const IconRain = (p: P) => <svg {...base} {...p}><path d="M7 15a5 5 0 1 1 1-9.9A6 6 0 0 1 19 8a4 4 0 0 1-1 7.9" /><path d="M8 18l-1 3M12 18l-1 3M16 18l-1 3" /></svg>
export const IconSun = (p: P) => <svg {...base} {...p}><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></svg>
export const IconCloud = (p: P) => <svg {...base} {...p}><path d="M7 18a5 5 0 1 1 1-9.9A6 6 0 0 1 19 11a4 4 0 0 1-1 7H7Z" /></svg>
export const IconWalk = (p: P) => <svg {...base} {...p}><circle cx="13" cy="4" r="1.8" /><path d="M10 21l2-6 3 3v3M9 11l3-3 3 3 3 1M12 8l-1 7" /></svg>
export const IconShield = (p: P) => <svg {...base} {...p}><path d="M12 3 5 6v5c0 4.5 3 8.3 7 10 4-1.7 7-5.5 7-10V6Z" /><path d="m9 12 2 2 4-4" /></svg>
export const IconUnlock = (p: P) => <svg {...base} {...p}><rect x="4" y="11" width="16" height="10" rx="2" /><path d="M8 11V7a4 4 0 0 1 7.5-2" /></svg>
export const IconThumb = (p: P) => <svg {...base} {...p}><path d="M7 11v9H4v-9h3Zm0 0 4-8a2 2 0 0 1 2 2v4h6a2 2 0 0 1 2 2.3l-1.3 7A2 2 0 0 1 17.7 20H7" /></svg>
export const IconAccessible = (p: P) => <svg {...base} {...p}><circle cx="12" cy="4" r="1.8" /><path d="M9 8h6M12 8v6h4l2 5" /><path d="M9.5 11.5a5 5 0 1 0 6.3 6.3" /></svg>
export const IconUmbrella = (p: P) => <svg {...base} {...p}><path d="M3 12a9 9 0 0 1 18 0Z" /><path d="M12 12v6a2 2 0 0 0 4 0" /></svg>
export const IconBolt = (p: P) => <svg {...base} {...p}><path d="M13 3 5 14h6l-1 7 8-11h-6Z" /></svg>
export const IconRamp = (p: P) => <svg {...base} {...p}><path d="M3 19h18L3 11Z" /></svg>
export const IconCheck = (p: P) => <svg {...base} {...p}><path d="m5 12 4 4 10-10" /></svg>
export const IconAlert = (p: P) => <svg {...base} {...p}><path d="M12 3 2 20h20Z" /><path d="M12 10v4M12 17h0" /></svg>
export const IconSliders =(p: P) => <svg {...base} {...p}><path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0" /><circle cx="16" cy="6" r="2" /><circle cx="10" cy="12" r="2" /><circle cx="18" cy="18" r="2" /></svg>
