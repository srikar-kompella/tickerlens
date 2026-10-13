import { describe, expect, it } from 'vitest'
import {
  fmtAge,
  fmtCompact,
  fmtDate,
  fmtDateTime,
  fmtMarketCap,
  fmtMoney,
  fmtNum,
  fmtPct,
  plColor,
} from './format'

describe('fmtMoney', () => {
  it('handles null and undefined', () => {
    expect(fmtMoney(null)).toBe('—')
    expect(fmtMoney(undefined)).toBe('—')
  })

  it('formats normal values with default digits', () => {
    expect(fmtMoney(1234.56)).toBe('$1,234.56')
    expect(fmtMoney(0)).toBe('$0.00')
  })

  it('formats negative numbers', () => {
    expect(fmtMoney(-50.25)).toBe('-$50.25')
  })

  it('formats custom digits and very large or small numbers', () => {
    expect(fmtMoney(100, 0)).toBe('$100')
    expect(fmtMoney(1_000_000_000.5, 2)).toBe('$1,000,000,000.50')
    expect(fmtMoney(0.0005, 4)).toBe('$0.0005')
  })
})

describe('fmtPct', () => {
  it('handles null and undefined', () => {
    expect(fmtPct(null)).toBe('—')
    expect(fmtPct(undefined)).toBe('—')
  })

  it('formats normal and negative percentages', () => {
    expect(fmtPct(12.345, 2)).toBe('12.35%')
    expect(fmtPct(-4.5, 1)).toBe('-4.5%')
    expect(fmtPct(0, 2)).toBe('0.00%')
  })

  it('formats signed positive percentages', () => {
    expect(fmtPct(8.5, 2, true)).toBe('+8.50%')
    expect(fmtPct(-8.5, 2, true)).toBe('-8.50%')
    expect(fmtPct(0, 2, true)).toBe('0.00%')
  })
})

describe('fmtNum', () => {
  it('handles null and undefined', () => {
    expect(fmtNum(null)).toBe('—')
    expect(fmtNum(undefined)).toBe('—')
  })

  it('formats normal, negative, and large numbers', () => {
    expect(fmtNum(1234567.89)).toBe('1,234,567.89')
    expect(fmtNum(-999.5)).toBe('-999.5')
    expect(fmtNum(0)).toBe('0')
    expect(fmtNum(0.123456, 3)).toBe('0.123')
  })
})

describe('fmtMarketCap', () => {
  it('handles null and undefined', () => {
    expect(fmtMarketCap(null)).toBe('—')
    expect(fmtMarketCap(undefined)).toBe('—')
  })

  it('formats values in trillions, billions, and millions', () => {
    expect(fmtMarketCap(2_500_000)).toBe('$2.50T')
    expect(fmtMarketCap(1_000_000)).toBe('$1.00T')
    expect(fmtMarketCap(15_400)).toBe('$15.4B')
    expect(fmtMarketCap(1_000)).toBe('$1.0B')
    expect(fmtMarketCap(850)).toBe('$850M')
    expect(fmtMarketCap(0)).toBe('$0M')
  })
})

describe('fmtCompact', () => {
  it('handles null and undefined', () => {
    expect(fmtCompact(null)).toBe('—')
    expect(fmtCompact(undefined)).toBe('—')
  })

  it('formats compact large numbers', () => {
    expect(fmtCompact(1_500_000)).toBe('1.5M')
    expect(fmtCompact(25_000)).toBe('25K')
  })
})

describe('fmtAge', () => {
  it('handles null and undefined', () => {
    expect(fmtAge(null)).toBe('')
    expect(fmtAge(undefined)).toBe('')
  })

  it('formats seconds, minutes, and hours', () => {
    expect(fmtAge(15)).toBe('15s ago')
    expect(fmtAge(120)).toBe('2m ago')
    expect(fmtAge(7200)).toBe('2.0h ago')
  })
})

describe('fmtDate and fmtDateTime', () => {
  it('handles null, undefined, and invalid strings', () => {
    expect(fmtDate(null)).toBe('—')
    expect(fmtDate(undefined)).toBe('—')
    expect(fmtDate('not-a-date')).toBe('not-a-date')

    expect(fmtDateTime(null)).toBe('—')
    expect(fmtDateTime(undefined)).toBe('—')
    expect(fmtDateTime('not-a-date')).toBe('not-a-date')
  })

  it('formats valid ISO dates', () => {
    const formatted = fmtDate('2026-07-14T12:00:00Z')
    expect(formatted).toContain('2026')
    expect(formatted).toContain('Jul')
  })
})

describe('plColor', () => {
  it('returns text-muted for null, undefined, or zero', () => {
    expect(plColor(null)).toBe('text-muted')
    expect(plColor(undefined)).toBe('text-muted')
    expect(plColor(0)).toBe('text-muted')
  })

  it('returns text-up for positive values', () => {
    expect(plColor(10.5)).toBe('text-up')
  })

  it('returns text-down for negative values', () => {
    expect(plColor(-4.2)).toBe('text-down')
  })
})
