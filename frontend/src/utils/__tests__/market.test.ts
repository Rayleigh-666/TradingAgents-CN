import { describe, it, expect } from 'vitest'
import { getMarketByStockCode, looksLikeInstrumentCode } from '../market'

describe('getMarketByStockCode', () => {
  describe('A股识别', () => {
    it('应该识别6位数字为A股', () => {
      expect(getMarketByStockCode('000001')).toBe('A股')
      expect(getMarketByStockCode('600519')).toBe('A股')
      expect(getMarketByStockCode('300750')).toBe('A股')
      expect(getMarketByStockCode('688981')).toBe('A股')
    })
  })

  describe('边界情况', () => {
    it('应该处理空字符串', () => {
      expect(getMarketByStockCode('')).toBe('A股') // 默认返回A股
    })
  })
})

describe('looksLikeInstrumentCode', () => {
  describe('自由文本必须排除', () => {
    it('中文自由文本应判为 false', () => {
      expect(looksLikeInstrumentCode('A股市场当日盘面复盘')).toBe(false)
      expect(looksLikeInstrumentCode('当前A股市场情绪周期定位与仓位建议')).toBe(false)
    })

    it('含空白的文本应判为 false', () => {
      expect(looksLikeInstrumentCode('hello world')).toBe(false)
    })
  })

  describe('真实代码应判为 true', () => {
    it('A股 6 位数字', () => {
      expect(looksLikeInstrumentCode('600519')).toBe(true)
      expect(looksLikeInstrumentCode('000001')).toBe(true)
    })

    it('带后缀代码', () => {
      expect(looksLikeInstrumentCode('000001.SZ')).toBe(true)
      expect(looksLikeInstrumentCode('600519.SS')).toBe(true)
      expect(looksLikeInstrumentCode('830799.BJ')).toBe(true)
    })

    it('港股', () => {
      expect(looksLikeInstrumentCode('00700.HK')).toBe(true)
      expect(looksLikeInstrumentCode('00700')).toBe(true)
    })

    it('美股字母代码', () => {
      expect(looksLikeInstrumentCode('AAPL')).toBe(true)
      expect(looksLikeInstrumentCode('BRK.B')).toBe(true)
    })
  })

  describe('已知误报边界（有意保留，用例锁死行为）', () => {
    it('4-5 位纯数字会被判成港股（如年份 "2024"）', () => {
      expect(looksLikeInstrumentCode('2024')).toBe(true)
    })

    it('1-5 位纯字母会被判成美股（如英文词 "GOLD"）', () => {
      expect(looksLikeInstrumentCode('GOLD')).toBe(true)
    })

    it('前端故意拒绝 1-3 位纯数字（后端会接受为港股，如 "700"）', () => {
      expect(looksLikeInstrumentCode('700')).toBe(false)
      expect(looksLikeInstrumentCode('36')).toBe(false)
    })
  })
})

