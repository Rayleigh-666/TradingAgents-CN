// 市场参数规范化：V3.0 聚焦 A 股，港美股已暂停
export const normalizeMarketForAnalysis = (market: any): string => {
  const raw = String(market ?? '').trim()
  const upper = raw.toUpperCase()
  const cn = raw
  const isETF = ['ETF', '场内基金'].includes(cn) || upper === 'ETF'
  const isA = [
    'A股', '主板', '创业板', '科创板', '中小板', '沪市', '深市', '上交所', '深交所', '北交所'
  ].includes(cn) || ['CN', 'SH', 'SZ', 'SSE', 'SZSE'].includes(upper)
  if (isETF) return 'ETF'
  if (isA) return 'A股'
  // 默认按A股处理
  return 'A股'
}

/**
 * 将交易所代码转换为市场类型
 * @param exchangeCode 交易所代码（如 "sz", "sh", "hk", "us"）
 * @returns 市场类型（"A股", "港股", "美股"）
 */
export const exchangeCodeToMarket = (exchangeCode: string): string => {
  const code = String(exchangeCode ?? '').toLowerCase().trim()

  // A股交易所代码
  if (['sz', 'sh', 'bj', 'sse', 'szse', 'bse'].includes(code)) {
    return 'A股'
  }

  // 默认返回A股
  return 'A股'
}

/**
 * 根据股票代码判断市场类型
 * @param stockCode 股票代码
 * @returns 市场类型（"A股", "港股", "美股", "ETF"）
 */
export const getMarketByStockCode = (stockCode: string): string => {
  const code = String(stockCode ?? '').trim().toUpperCase()

  // 6位数字：先判断 ETF，否则 A股
  if (/^\d{6}$/.test(code)) {
    const prefix = code.substring(0, 2)
    if (['50', '51', '52', '56', '58', '15', '16', '18'].includes(prefix)) {
      return 'ETF'
    }
    return 'A股'
  }

  // 默认返回A股
  return 'A股'
}

/**
 * 把各种市场写法归一化为后端同步/校验接口的 market_type（cn/hk/us）。
 *
 * 注意：`normalizeMarketForAnalysis` 是 V3.0「仅 A 股」时代的助手，会把港美股
 * 一律降级成 A股，**不可**用于数据同步/校验（会把港股请求打到 A 股链路上）。
 */
export const toApiMarketType = (market: any): 'cn' | 'hk' | 'us' => {
  const raw = String(market ?? '').trim().toUpperCase()

  if (['HK', 'HKG', 'HK_STOCKS', 'HONGKONG', 'HONG KONG', '港股', '香港'].includes(raw)) {
    return 'hk'
  }
  if (['US', 'USA', 'US_STOCKS', '美股', '美国'].includes(raw)) {
    return 'us'
  }
  return 'cn'
}

/**
 * 判断字符串是否「看起来像」一个可识别的证券代码。
 *
 * 仅用于通用研究场景：用户填的是自由文本分析目标（如「A股市场当日盘面复盘」），
 * 只有看起来像真实代码时才允许把它复用为 ticker，避免自由文本污染下游 symbol。
 *
 * 命中：A股 6 位数字（600519 / 000001）、带后缀（600519.SH / 000001.SZ / 600519.SS /
 * 830799.BJ）、港股（00700.HK 或 4-5 位数字 00700 / 9988）、美股字母代码（AAPL / BRK.B）。
 * 排除：含中文/空白或过长的自由文本。
 */
export function looksLikeInstrumentCode(input: string): boolean {
  const s = String(input ?? '').trim()
  if (!s) return false
  // 含非 ASCII（中文等）或空白 → 一定是自由文本
  if (/[^\x00-\x7F]/.test(s)) return false
  if (/\s/.test(s)) return false

  const upper = s.toUpperCase()

  // A股：6 位数字，或带沪深/北交所后缀
  if (/^\d{6}$/.test(upper)) return true
  if (/^\d{6}\.(SH|SZ|SS|BJ|XSHG|XSHE)$/.test(upper)) return true

  // 港股：1-5 位数字.HK，或纯 4-5 位数字
  if (/^\d{1,5}\.HK$/.test(upper)) return true

  // ⚠️ 已知误报边界（有意保留，不修）：
  //   · 纯 4-5 位数字 → "2024" 这类年份/编号会被误判成港股；
  //   · 1-5 位纯字母   → "GOLD" 这类英文词会被误判成美股。
  //   仍接受，是因为本函数只回答「要不要把分析目标复用为 ticker」：它宁可**漏判**
  //   （把真代码拒掉、退化为自由文本）也不愿把自由文本放行成代码；且真正定市场/定代码
  //   的是后端 app/core/market_codes.resolve_instrument（二次形态校验 + 归一化）。
  if (/^\d{4,5}$/.test(upper)) return true

  // 美股：1-5 位字母，可带股份类别（BRK.B / BRK-B）
  if (/^[A-Z]{1,5}$/.test(upper)) return true
  if (/^[A-Z]{1,5}[.\-][A-Z]$/.test(upper)) return true

  // 跨端一致性：后端 app/core/market_codes.looks_like_instrument_code 与本函数
  // **故意在 1-3 位纯数字上有别** —— 后端接受 \d{1,5}（铁律：纯数字 ≤5 位 → 港股，
  // "700" 是最常见的腾讯写法），前端为「宁缺勿滥」只接受 4-5 位。
  // 改动任一侧请同步另一侧的注释。
  return false
}

export default {
  normalizeMarketForAnalysis,
  exchangeCodeToMarket,
  getMarketByStockCode,
  toApiMarketType,
  looksLikeInstrumentCode
}

