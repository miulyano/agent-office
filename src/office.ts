// Names, money and durations shared by the page and the floors view.

import type { Office, OfficeDesk } from './types'

// Built-in agents have no desk of their own: they work on the free-lance floor.
export const GUESTS: Record<string, { name: string; shirt: string }> = {
  'general-purpose': { name: 'Фрилансер', shirt: '#6a6f7a' },
  Explore: { name: 'Разведчик', shirt: '#4a6a5a' },
  Plan: { name: 'Архитектор', shirt: '#4a4a7a' },
  claude: { name: 'Фрилансер', shirt: '#6a6f7a' },
}

// how long, with units: a bare 1:05 could be minutes or hours
export function span(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000))
  if (s < 60) return `${s}с`
  if (s < 3600) return `${Math.floor(s / 60)}м`
  return `${Math.floor(s / 3600)}ч ${String(Math.floor(s / 60) % 60).padStart(2, '0')}м`
}

// the total worked by now: the server sends what is closed and when the running stretch began
export const workedNow = (x: { worked?: number; workingSince?: number }, now: number) =>
  (x.worked ?? 0) + (x.workingSince ? Math.max(0, now - x.workingSince) : 0)

export function roleKey(type: string): string {
  const i = type.lastIndexOf(':')
  return i === -1 ? type : type.slice(i + 1)
}

// Who sits where: office desks by full type, then by the bare key.
export function deskOf(office: Office, type: string): OfficeDesk | undefined {
  const all = office.zones.flatMap(z => z.desks)
  return all.find(d => d.type === type) ?? all.find(d => d.type !== 'main' && roleKey(d.type) === roleKey(type))
}

export function roleName(type: string, office?: Office): string {
  if (type === 'producer' || type === 'main') return office ? deskOf(office, 'main')?.name ?? 'Главный' : 'Главный'
  const desk = office && deskOf(office, type)
  if (desk) return desk.name
  const guest = office?.guests[type] ?? GUESTS[type]
  if (guest) return guest.name
  const key = roleKey(type)
  return key.length > 12 ? key.slice(0, 11) + '…' : key
}

// The label on a desk: the short form where the full title does not fit.
export function deskName(type: string, office?: Office): string {
  const desk = office && deskOf(office, type === 'producer' ? 'main' : type)
  return desk?.short ?? roleName(type, office)
}

export function money(usd: number): string {
  if (usd > 0 && usd < 0.05) return '<$0.1' // a few cents are not «$0.0»
  return '$' + usd.toFixed(1).replace(/\B(?=(\d{3})+\.)/g, '\u00a0')
}
