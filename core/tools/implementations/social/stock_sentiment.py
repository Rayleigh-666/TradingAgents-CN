"""
统一股票情绪分析工具

自动识别股票类型（A股、港股、美股）并调用相应的情绪数据源
优先使用用户上传的社媒数据
"""

import logging
import os
from typing import Annotated, List, Optional, Tuple
from datetime import datetime, timedelta
from langchain_core.tools import tool

from app.core.market_codes import (
    normalize_symbol,
    resolve_instrument,
    try_resolve_market,
)
from core.tools.base import register_tool

logger = logging.getLogger(__name__)


# ==== 市场识别 ====
# 市场别名的**唯一真源**是 app/core/market_codes.py::_MARKET_ALIASES，本文件不再自带
# 别名表（历史漂移点：一侧写 a_share、另一侧 a_shares，同一输入命中结果不同）。
# 统一走 try_resolve_market —— 它对未知写法返回 ""（resolve_market 会兜底 "cn"），
# 正好满足本文件「市场未识别须做显式降级说明」的需求。
#
# ⚠️ 代码形态识别一律走 resolve_instrument：它内部**先做形态判断**再定市场。
# 绝不能把可疑自由文本直接喂给 guess_market —— guess_market 的规则是「含字母 → us」，
# 而自由文本「A股市场当日盘面复盘」含拉丁字母 A，会被误判成美股。
_MARKET_NAMES = {"cn": "中国A股", "hk": "港股", "us": "美股"}


def _resolve_market(market_type: str, ticker: str) -> Tuple[str, str, bool, bool, bool]:
    """解析本次分析应使用的目标市场。

    优先级（**显式 market_type 优先于代码形态识别**，以保留既有语义）：
      1. ``try_resolve_market(market_type)`` —— 用户/上游显式指定（大小写不敏感，
         容错 cn/a/中国/A股/港股/hongkong/us/美股 等写法；未知或未给 → ""）；
      2. ``resolve_instrument(ticker)`` 的形态识别 —— 先判「像不像证券代码」再定市场
         （"700" → hk、"0700.HK" → hk、"600519" → cn、"AAPL" → us；
         自由文本 / MARKET / __GENERAL__ → 不像代码，返回 ""）；
      3. 两者都没有 → ``market_key == ""``，表示**市场未识别**，
         调用方须做显式降级说明（不要静默按 A 股处理）。

    Args:
        market_type: 可选的市场类型修正值（cn/hk/us 及其各种写法）
        ticker: 股票代码 / 自由文本标的

    Returns:
        Tuple[str, str, bool, bool, bool]:
            (market_key, market_name, is_china, is_hk, is_us)
            market_key ∈ {"cn", "hk", "us"}；``""`` 表示市场未识别。
    """
    explicit = try_resolve_market(market_type)
    if market_type and not explicit:
        logger.warning(
            "⚠️ [统一情绪工具] 无法识别的 market_type=%r，已忽略并回落到按标的识别",
            market_type,
        )

    code_market, _symbol = resolve_instrument(ticker)

    # 显式 market_type 与代码形态识别冲突时（如 market_type="us" + A 股代码 "600519"），
    # 仍以显式值为准（保留既有语义），但必须告警——否则冲突会被静默吞掉，直到下游
    # 数据层（如美股 Reddit）报错才暴露。主链路 app/routers/workflows.py 在冲突时也告警。
    if explicit and code_market and explicit != code_market:
        logger.warning(
            "⚠️ [统一情绪工具] 显式 market_type=%r 与标的 %r 识别的市场 %r 冲突，"
            "已按显式 market_type 处理",
            market_type,
            ticker,
            code_market,
        )

    market_key = explicit or code_market
    if not market_key:
        return "", "市场未识别", False, False, False

    market_name = _MARKET_NAMES[market_key]
    return (
        market_key,
        market_name,
        market_key == "cn",
        market_key == "hk",
        market_key == "us",
    )


def _build_chinese_sentiment(
    ticker: str, curr_date: str, market_name: str, market: str
) -> List[str]:
    """构建中文市场（A股 / 港股，以及无法识别标的的兜底）情绪摘要列表。

    Args:
        ticker: 股票代码 / 自由文本标的
        curr_date: 当前日期（格式 YYYY-MM-DD）
        market_name: 用于展示的市场中文名
        market: 已确定的内部市场 key（"cn" / "hk" / ""）。**只有 "cn" 允许走
            akshare 备用源**；"hk" 与 "" 在结构上被禁止进入该分支
            （akshare 的 ak.stock_news_em 只认 A 股 6 位码，港股 00700 会被
             zfill(6) 成 000700 —— 另一只股票）。
    """
    result_data: List[str] = []
    logger.info(f"🇨🇳🇭🇰 [统一情绪工具] 处理中文市场情绪（市场: {market_name}）...")

    # P2-7：这些变量在下面 try 内才会被首次赋值，若 try 里早期（如 get_mongo_db_sync）
    # 就抛异常，except 的诊断日志会因引用未绑定变量而抛 NameError，把真实错误掩盖掉。
    # 先在此初始化，保证 except 里的日志永远可执行。
    clean_ticker = ""
    start_time = None
    end_time = None

    # 🔥 优先查询用户上传的社媒数据（使用同步方式）
    user_uploaded_data = None
    try:
        logger.info(f"🔍 [统一情绪工具] 开始查询数据库中的社媒数据...")
        from app.core.database import get_mongo_db_sync

        # 铁律：内部 symbol 口径 cn→6 位、hk→5 位、us→裸 ticker。
        # 查库前一律走 normalize_symbol，**禁止**业务侧手工剥后缀 / zfill。
        # （历史 bug：手工剥 ".HK" 把 0700.HK 变成 4 位 0700，与库内 5 位 00700
        #   永远匹配不上，查 social_media_messages 100% miss。）
        # market 为空（未识别兜底）时不做任何查询 —— 拿空串查库只会静默 miss。
        clean_ticker = normalize_symbol(ticker, market) if market else ""

        if not clean_ticker:
            logger.warning(
                "⚠️ [统一情绪工具] 标的 %r 在市场 %r 下无法归一化为内部 symbol，跳过社媒数据查询",
                ticker,
                market or market_name,
            )
            result_data.append(
                f"## 中文市场情绪\n标的「{ticker}」无法归一化为可查询的内部 symbol"
                f"（市场：{market_name}），已跳过社媒数据查询。"
            )
        else:
            # 使用同步方式查询数据库
            db = get_mongo_db_sync()
            collection = db.social_media_messages

            # 查询最近7天的社媒数据
            end_time = datetime.utcnow()
            start_time = end_time - timedelta(days=7)

            query = {
                "symbol": clean_ticker,
                "publish_time": {
                    "$gte": start_time,
                    "$lte": end_time
                }
            }

            # 同步查询
            logger.info(f"🔍 [统一情绪工具] 查询数据库: collection=social_media_messages, symbol={clean_ticker}, time_range=最近7天")
            logger.debug(f"🔍 [统一情绪工具] 查询条件详情: {query}")
            cursor = collection.find(query).sort("publish_time", -1).limit(100)
            user_messages = list(cursor)
            logger.info(f"📊 [统一情绪工具] 数据库查询完成: 找到 {len(user_messages)} 条消息")

            if user_messages and len(user_messages) > 0:
                logger.info(f"✅ [统一情绪工具] 找到 {len(user_messages)} 条用户上传的社媒数据，开始分析...")
                user_uploaded_data = user_messages

                # 🔥 详细分析用户上传的社媒数据
                sentiment_counts = {"positive": 0, "negative": 0, "neutral": 0}
                platform_counts = {}
                recent_messages = []
                keyword_counts = {}
                hashtag_counts = {}
                total_engagement = {"views": 0, "likes": 0, "shares": 0, "comments": 0}
                high_influence_messages = []

                for msg in user_messages:
                    sentiment = msg.get("sentiment", "neutral")
                    sentiment_counts[sentiment] += 1

                    platform = msg.get("platform", "unknown")
                    platform_counts[platform] = platform_counts.get(platform, 0) + 1

                    # 统计关键词
                    keywords = msg.get("keywords", [])
                    if isinstance(keywords, list):
                        for kw in keywords:
                            keyword_counts[kw] = keyword_counts.get(kw, 0) + 1

                    # 统计话题标签
                    hashtags = msg.get("hashtags", [])
                    if isinstance(hashtags, list):
                        for tag in hashtags:
                            hashtag_counts[tag] = hashtag_counts.get(tag, 0) + 1

                    # 统计互动数据
                    engagement = msg.get("engagement", {})
                    if isinstance(engagement, dict):
                        total_engagement["views"] += engagement.get("views", 0)
                        total_engagement["likes"] += engagement.get("likes", 0)
                        total_engagement["shares"] += engagement.get("shares", 0)
                        total_engagement["comments"] += engagement.get("comments", 0)

                    # 收集高影响力消息（影响力评分>50或互动率高）
                    author = msg.get("author", {})
                    influence_score = author.get("influence_score", 0) if isinstance(author, dict) else 0
                    engagement_rate = engagement.get("engagement_rate", 0) if isinstance(engagement, dict) else 0

                    if influence_score > 50 or engagement_rate > 5.0:
                        if len(high_influence_messages) < 3:
                            high_influence_messages.append({
                                "content": msg.get("content", "")[:150] + "..." if len(msg.get("content", "")) > 150 else msg.get("content", ""),
                                "platform": platform,
                                "sentiment": sentiment,
                                "influence_score": influence_score,
                                "engagement_rate": engagement_rate,
                                "author": author.get("author_name", "未知") if isinstance(author, dict) else "未知"
                            })

                    # 收集最近消息摘要（优先高影响力）
                    content = msg.get("content", "")
                    if content and len(recent_messages) < 10:
                        recent_messages.append({
                            "content": content[:100] + "..." if len(content) > 100 else content,
                            "platform": platform,
                            "sentiment": sentiment,
                            "publish_time": msg.get("publish_time", ""),
                            "influence_score": influence_score
                        })

                # 按影响力排序最近消息
                recent_messages.sort(key=lambda x: x.get("influence_score", 0), reverse=True)
                recent_messages = recent_messages[:5]  # 只保留前5条

                # 计算情绪评分
                total = sum(sentiment_counts.values())
                if total > 0:
                    sentiment_score_val = (sentiment_counts["positive"] - sentiment_counts["negative"]) / total
                    if sentiment_score_val > 0.1:
                        sentiment_score = "偏多"
                    elif sentiment_score_val < -0.1:
                        sentiment_score = "偏空"
                    else:
                        sentiment_score = "中性"
                else:
                    sentiment_score = "中性"

                # 构建详细报告
                platform_summary = ", ".join([f"{k}({v})" for k, v in sorted(platform_counts.items(), key=lambda x: x[1], reverse=True)[:5]])

                # 热门关键词
                top_keywords = sorted(keyword_counts.items(), key=lambda x: x[1], reverse=True)[:5]
                keywords_summary = ", ".join([f"{k}({v})" for k, v in top_keywords]) if top_keywords else "无"

                # 热门话题
                top_hashtags = sorted(hashtag_counts.items(), key=lambda x: x[1], reverse=True)[:5]
                hashtags_summary = ", ".join([f"#{k}({v})" for k, v in top_hashtags]) if top_hashtags else "无"

                # 互动数据汇总
                total_interactions = sum(total_engagement.values())
                engagement_summary = f"总浏览量:{total_engagement['views']}, 点赞:{total_engagement['likes']}, 转发:{total_engagement['shares']}, 评论:{total_engagement['comments']}"

                # 高影响力消息
                high_influence_summary = ""
                if high_influence_messages:
                    # 构建高影响力消息列表（避免在f-string中使用反斜杠）
                    influence_lines = []
                    for m in high_influence_messages:
                        line = f"- [{m['platform']}] [{m['sentiment']}] 作者:{m['author']} (影响力:{m['influence_score']:.1f}, 互动率:{m['engagement_rate']:.2f}%)"
                        content_line = f"  {m['content']}"
                        influence_lines.append(f"{line}\n{content_line}")
                    high_influence_summary = f"""
### 高影响力消息（影响力评分>50或互动率>5%）
{chr(10).join(influence_lines)}
"""

                sentiment_summary = f"""## 社媒情绪分析（用户上传数据）
**股票**: {ticker}
**数据来源**: 用户上传的社媒内容
**数据量**: 共 {len(user_messages)} 条消息（最近7天）
**情绪评级**: {sentiment_score} (正向:{sentiment_counts['positive']}, 中性:{sentiment_counts['neutral']}, 负向:{sentiment_counts['negative']})
**平台分布**: {platform_summary}
**热门关键词**: {keywords_summary}
**热门话题**: {hashtags_summary}
**互动数据**: {engagement_summary}（总互动:{total_interactions}）

### 最近社媒消息摘要（按影响力排序）
{chr(10).join([f"- [{m['platform']}] [{m['sentiment']}] (影响力:{m.get('influence_score', 0):.1f}) {m['content']}" for m in recent_messages])}
{high_influence_summary}

*基于用户上传的社媒数据，数据质量更高，分析更准确*
"""
                result_data.append(sentiment_summary)

    except Exception as e:
        logger.warning(f"⚠️ [统一情绪工具] 查询用户上传社媒数据失败: {e}", exc_info=True)
        logger.info(f"🔍 [统一情绪工具] 查询参数: symbol={clean_ticker}, start_time={start_time}, end_time={end_time}")
        # 继续使用备用数据源

    # 如果没有用户上传的数据或数据不足，使用备用数据源
    if not user_uploaded_data or len(user_uploaded_data) < 5:
        if user_uploaded_data:
            logger.info(f"📰 [统一情绪工具] 用户上传数据不足（{len(user_uploaded_data)}条 < 5条），使用备用数据源（新闻）...")
        else:
            logger.info(f"📰 [统一情绪工具] 未找到用户上传的社媒数据（数据库查询返回0条或查询失败），使用备用数据源（新闻）...")

        # P1-4：**只有 A 股（market == "cn"）** 才允许走 akshare 备用源。
        # akshare 的 ak.stock_news_em(symbol=...) 只认 A 股 6 位码；港股传 00700 会被
        # zfill(6) 成 000700（另一只股票）。因此港股/未识别标的在结构上禁止进入该分支
        # —— 不是靠注释约定，而是直接 return 提前退出。
        if market != "cn" or not clean_ticker:
            if not user_uploaded_data:
                if market == "hk":
                    result_data.append(
                        "## 港股舆情\n"
                        "港股舆情暂无本地数据源（不主动拉取 A 股新闻接口）。\n\n"
                        "*说明：这是「数据源不可用」，**不代表情绪中性**，请勿据此判断市场情绪。*"
                    )
                else:
                    # 未识别市场（含 A 股但无法归一化）兜底：必须与港股 / 美股分支一致地
                    # 显式标注「数据源不可用 ≠ 情绪中性」，避免被下游误读成中性。
                    result_data.append(
                        "## 中文市场情绪\n"
                        "暂无相关数据（建议上传社媒数据）。\n\n"
                        "*说明：这是「数据源不可用」，**不代表情绪中性**，请勿据此判断市场情绪。*"
                    )
            return result_data

        try:
            # 尝试使用 AKShareProvider 获取新闻进行情绪分析
            from tradingagents.dataflows.providers.china.akshare import AKShareProvider
            ak_provider = AKShareProvider()

            # 统一使用归一化后的内部 symbol（A 股 6 位），不再手工剥后缀。
            # 获取新闻 (优先使用 _get_stock_news_direct 如果公开接口不可用)
            if hasattr(ak_provider, 'get_stock_news_sync'):
                news_df = ak_provider.get_stock_news_sync(symbol=clean_ticker)
            else:
                news_df = ak_provider._get_stock_news_direct(symbol=clean_ticker)

            if news_df is not None and not news_df.empty:
                # 简单分析
                news_titles = []
                # 兼容不同的列名
                col_map = {'新闻标题': 'title', '标题': 'title', 'title': 'title'}

                for col in news_df.columns:
                    if col in col_map:
                        news_titles = news_df[col].tolist()
                        break

                if not news_titles and not news_df.empty:
                    # 如果没找到标题列，尝试第一列
                    news_titles = news_df.iloc[:, 0].astype(str).tolist()

                # 关键词统计
                pos_words = ['上涨', '利好', '增长', '突破', '买入', '增持', '大涨', '新高', '预增']
                neg_words = ['下跌', '利空', '减少', '跌破', '卖出', '减持', '大跌', '新低', '亏损']

                pos_count = 0
                neg_count = 0

                recent_titles = news_titles[:10]  # 只看最近10条

                for title in recent_titles:
                    if not isinstance(title, str):
                        continue
                    for w in pos_words:
                        if w in title:
                            pos_count += 1
                    for w in neg_words:
                        if w in title:
                            neg_count += 1

                sentiment_score = "中性"
                if pos_count > neg_count:
                    sentiment_score = "偏多"
                if neg_count > pos_count:
                    sentiment_score = "偏空"

                news_summary = f"""## 新闻情绪分析（备用数据源）
**股票**: {ticker}
**情绪评级**: {sentiment_score} (正向词:{pos_count}, 负向词:{neg_count})

### 最近新闻摘要
{chr(10).join(['- ' + str(t) for t in recent_titles[:5]])}

*基于最近 {len(recent_titles)} 条新闻标题分析*
"""
                result_data.append(news_summary)
            else:
                if not user_uploaded_data:
                    result_data.append(f"## 中文市场情绪\n暂无相关数据（建议上传社媒数据）")

        except Exception as e:
            logger.error(f"中文情绪分析失败: {e}")
            if not user_uploaded_data:
                result_data.append(f"## 中文市场情绪\n分析失败: {e}，建议上传社媒数据")

    return result_data


def _reddit_cache_unavailable_reason(data_dir: Optional[str] = None) -> str:
    """返回「本地 Reddit 缓存不可用」的原因；缓存可用则返回空串。

    本函数是**美股情绪维度的唯一可用性门控**（调用点见 ``_build_us_sentiment``）。

    数据源真相：``get_reddit_company_news`` / ``get_reddit_global_news``
    （``tradingagents/dataflows/interface.py``）最终调
    ``tradingagents/dataflows/news/reddit.py::fetch_top_from_category``，
    **读的是本地缓存目录** ``os.path.join(DATA_DIR, "reddit_data")/<category>/*.jsonl``：
    不发网络请求，也**不读** ``REDDIT_CLIENT_ID`` / ``REDDIT_CLIENT_SECRET``。
    历史实现按 ``REDDIT_*`` 凭证判断是**错的门槛**——有凭证但缓存为空时照样空转，
    缓存已存在却没配凭证时反而被判「不可用」。故门控只认「本地缓存里有没有 .jsonl」。

    路径与数据源**同源**：数据源用的 ``DATA_DIR = config_manager.get_data_dir()``
    是 ``interface.py`` 的模块级常量，这里直接复用同一个 ``DATA_DIR``，不另写一份
    路径规则、也不每次重新执行 ``config_manager.get_data_dir()``——否则运行期
    ``set_data_dir`` 改过配置后，门控会与数据源实际读取的目录不一致而误判。

    Args:
        data_dir: 显式指定数据目录（**仅测试注入用**）；为 ``None`` 时读
            ``interface.DATA_DIR``。

    Returns:
        可用时返回空串；不可用时返回原因文本，供下游输出「本维度不可用」的显式
        降级说明（原因文本会出现在报告中，**不含**文件系统绝对路径）。
    """
    if data_dir is None:
        try:
            from tradingagents.dataflows.interface import DATA_DIR

            data_dir = DATA_DIR
        except Exception as e:  # noqa: BLE001 - 取路径失败一律按不可用处理
            return f"无法解析 Reddit 缓存目录：{e}"

    if not data_dir:
        return "数据目录未配置"

    reddit_dir = os.path.join(data_dir, "reddit_data")
    if not os.path.isdir(reddit_dir):
        # 区分「路径压根不存在」（没预热过）与「路径存在但不是目录」（配置写错了）——
        # 两者对使用者的指引不同，不能共用一句「目录不存在」。
        if os.path.exists(reddit_dir):
            return "本地 Reddit 缓存路径不是目录"
        return "本地 Reddit 缓存目录不存在"

    # ⚠️ ``os.walk`` 默认 ``onerror=None`` 会**静默吞掉**遍历错误（PermissionError 等），
    # 结果是「目录不可读」被报成「缓存为空」——把「读不到」说成「没有数据」，恰是本
    # 函数最不该犯的错（该分支曾因此长期不可达）。这里把错误收进列表：
    #   * 只要还能在别处找到 .jsonl，就仍判可用（单个分类目录坏掉不该拖垮整个维度）；
    #   * 只有「一个 .jsonl 都没找到、同时确实发生过读取错误」才报读取失败，
    #     绝不把不确定说成「没有数据」。
    walk_errors: List[OSError] = []
    try:
        for _root, _dirs, files in os.walk(reddit_dir, onerror=walk_errors.append):
            if any(name.endswith(".jsonl") for name in files):
                return ""
    except OSError as e:  # 兜底：walk 自身抛错（未走 onerror 回调的路径）
        return f"读取本地 Reddit 缓存目录失败：{e}"

    if walk_errors:
        return f"读取本地 Reddit 缓存目录失败：{walk_errors[0]}"
    return "本地 Reddit 缓存为空（无 .jsonl 文件）"


def _build_us_sentiment(ticker: str, curr_date: str) -> List[str]:
    """构建美股（Reddit）情绪摘要。

    Reddit 数据源真相：``tradingagents/dataflows/interface.py::get_reddit_company_news`` /
    ``get_reddit_global_news`` 最终调 ``tradingagents/dataflows/news/reddit.py::fetch_top_from_category``，
    读的是**本地缓存目录** ``os.path.join(DATA_DIR, "reddit_data")/<category>/*.jsonl``，
    **全程不读取 Reddit 客户端凭证（环境变量），也不发网络请求**。
    因此可用性门控必须基于「本地缓存是否存在」，而不是「凭证是否配置」
    （旧门控会误报不可用、并误导用户去配一个根本没被使用的凭证）。

    本维度不可用 / 不适用时，必须返回明确的降级说明，与「取到零条讨论」区分开，
    避免下游把「不可用」误读成「情绪中性」。
    """
    # P3：形态闸门 —— 自由文本不是证券代码，直接喂给 Reddit 会在
    # news/reddit.py 的 ticker_to_company[query] 抛 KeyError。
    code_market, _symbol = resolve_instrument(ticker)
    if not code_market:
        logger.warning(f"⚠️ [统一情绪工具] 标的 {ticker!r} 非证券代码形态，跳过 Reddit 个股情绪")
        return [
            "## 美股情绪（Reddit）\n"
            f"标的「{ticker}」非证券代码形态，Reddit 个股情绪不适用。\n\n"
            "*说明：这是「数据源不适用」，**不代表情绪中性**，请勿据此判断市场情绪。*"
        ]

    unavailable_reason = _reddit_cache_unavailable_reason()
    if unavailable_reason:
        logger.warning(f"⚠️ [统一情绪工具] 本地 Reddit 缓存不可用（{unavailable_reason}），美股情绪维度不可用")
        return [
            "## 美股情绪（Reddit）\n"
            f"本地 Reddit 缓存不可用（{unavailable_reason}），本维度不可用。\n\n"
            "*说明：这是「数据源不可用」，**不代表情绪中性**，请勿据此判断市场情绪。*\n"
            "（注：该数据源读的是本地缓存目录，不是凭证。）"
        ]

    try:
        from tradingagents.dataflows.interface import (
            get_reddit_company_news,
            get_reddit_global_news,
        )

        sections = []
        try:
            # 数据层只认**归一化后的裸 ticker**（reddit.py 的 ticker_to_company 以裸 ticker
            # 为键，如 "AAPL"）。形态闸门接受的 AAPL.O / BRK-B 若原样传入，会在
            # ticker_to_company[query] 抛 KeyError（表现为「个股新闻获取失败」）。
            # 因此传 resolve_instrument 归一化出的 _symbol；给用户看的文案仍保留原 ticker。
            company_news = get_reddit_company_news(_symbol, curr_date, 7, 10)
            sections.append(company_news or f"（未取到 {ticker} 的相关 Reddit 讨论）")
        except Exception as e:
            logger.warning(f"⚠️ [统一情绪工具] Reddit 个股新闻获取失败: {e}")
            sections.append(f"个股新闻获取失败: {e}")

        try:
            global_news = get_reddit_global_news(curr_date, 7, 10)
            sections.append(global_news or "（未取到 Reddit 大盘新闻）")
        except Exception as e:
            logger.warning(f"⚠️ [统一情绪工具] Reddit 大盘新闻获取失败: {e}")
            sections.append(f"大盘新闻获取失败: {e}")

        return ["## 美股情绪（Reddit）\n" + "\n\n".join(sections)]
    except Exception as e:
        logger.error(f"❌ [统一情绪工具] 美股情绪分析失败: {e}")
        return [f"## 美股情绪（Reddit）\n获取失败: {e}"]


@tool
@register_tool(
    tool_id="get_stock_sentiment_unified",
    name="统一股票情绪分析",
    description=(
        "获取股票市场情绪和社交媒体情绪，支持 A 股、港股、美股。"
        "这里的“情绪/信号”指新闻、社交媒体和市场舆论情绪，不是技术买卖信号、财务风险信号、公告风险事件或 Agent 能力缺口。"
    ),
    category="social",
    is_online=True,
    auto_register=True,
    capability_tags=["stock_sentiment", "sentiment", "market_sentiment", "social_media_sentiment", "public_opinion", "social", "event_driven"],
    tool_role_hint="specialized",
    output_shape="markdown",
    preferred_for=["sentiment_analysis", "social_media_monitoring", "market_sentiment_assessment"],
    when_to_use="当需要分析市场舆情、社交媒体情绪、投资者情绪和情绪面影响时使用。",
    when_not_to_use="不用于技术指标交叉信号、财务报表异常、现金流风险、监管处罚/诉讼等公告事件识别或基本面估值。",
)
def get_stock_sentiment_unified(
    ticker: Annotated[str, "股票代码（支持A股、港股、美股）"],
    curr_date: Annotated[str, "当前日期，格式：YYYY-MM-DD"],
    market_type: Annotated[str, "市场类型：cn/hk/us，可选。用于修正无法从代码识别的标的（如大盘/行业类分析）"] = "",
) -> str:
    """
    统一的股票情绪分析工具
    自动识别股票类型（A股、港股、美股）并调用相应的情绪数据源

    Args:
        ticker: 股票代码（如：000001、0700.HK、AAPL），也可能是自由文本标的（如「A股市场当日盘面复盘」）
        curr_date: 当前日期（格式：YYYY-MM-DD）
        market_type: 可选的市场类型修正（cn/hk/us）。非空且可映射时优先采用；
            为空或无法映射时回落到按 ticker 的代码形态识别。

    Returns:
        str: 情绪分析报告
    """
    logger.info(f"😊 [统一情绪工具] 分析股票: {ticker} (market_type={market_type!r})")

    try:
        # 解析目标市场：显式 market_type 优先，其次按代码形态识别；都识别不出 →
        # market_key == ""，走显式降级（中文数据源兜底 + 明确标注）。
        market_key, market_name, _is_china, _is_hk, _is_us = _resolve_market(market_type, ticker)

        logger.info(f"😊 [统一情绪工具] 股票类型: {market_name} (market_key={market_key!r})")

        result_data = []

        if market_key == "cn":
            # A 股：本地社媒数据 + akshare 新闻备用源
            result_data.extend(_build_chinese_sentiment(ticker, curr_date, market_name, "cn"))
        elif market_key == "hk":
            # 港股：只允许查本地社媒数据（social_media_messages），
            # **不**主动拉 A 股新闻接口（akshare 只认 A 股 6 位码）。
            logger.info(f"🇭🇰 [统一情绪工具] 处理港股情绪（仅本地社媒数据）...")
            result_data.extend(_build_chinese_sentiment(ticker, curr_date, market_name, "hk"))
        elif market_key == "us":
            # 美股：Reddit 情绪（本地缓存不可用 / 非代码形态时给出明确降级说明）
            logger.info(f"🇺🇸 [统一情绪工具] 处理美股情绪...")
            result_data.extend(_build_us_sentiment(ticker, curr_date))
        else:
            # 市场无法识别（如自由文本标的）：**不要**落到美股分支（历史 bug：
            # UNKNOWN 命中「美股 Reddit」并 import 不存在的 get_reddit_sentiment）。
            # 按中文市场数据源兜底（本项目流程以 A 股为主），并在输出中明确标注，不静默。
            logger.info(f"❓ [统一情绪工具] 市场无法识别，按中文市场数据源兜底...")
            result_data.append(
                f"> ⚠️ 市场未能识别：无法从标的「{ticker}」判断所属市场，"
                f"已按 **中文市场数据源** 兜底（本分析流程以 A 股为主）。"
            )
            result_data.extend(_build_chinese_sentiment(ticker, curr_date, "中文市场（兜底）", ""))

        # 组合所有数据
        combined_result = f"""# {ticker} 情绪分析

**股票类型**: {market_name}
**分析日期**: {curr_date}

{chr(10).join(result_data)}

---
*数据来源: 根据股票类型自动选择最适合的情绪数据源*
"""

        logger.info(f"😊 [统一情绪工具] 数据获取完成，总长度: {len(combined_result)}")
        return combined_result

    except Exception as e:
        error_msg = f"统一情绪分析工具执行失败: {str(e)}"
        logger.error(f"❌ [统一情绪工具] {error_msg}")
        return error_msg
