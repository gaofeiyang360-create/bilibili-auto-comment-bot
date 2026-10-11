#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
B站自动点赞工具（完整增强版）
- 多账号负载均衡，独立风控
- 新增 up_blacklist：不处理指定 UP 主的视频
- 默认 target_up 为空，up_blacklist 为空
"""

import requests
import urllib.parse
import time
import random
import re
import json
import os
import hashlib
import argparse
import sys
import logging
from typing import List, Dict, Tuple, Optional, Union

# ---------- 获取脚本所在目录 ----------
def get_base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    else:
        return os.path.dirname(os.path.abspath(__file__))

SCRIPT_DIR = get_base_dir()

# ---------- 文件路径常量 ----------
COOKIE_FILE = os.path.join(SCRIPT_DIR, "cookie.txt")
DONE_FILE = os.path.join(SCRIPT_DIR, "done_bvid.json")
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")

# ---------- 日志相关 ----------
logger = None

def setup_logging(log_enabled: bool, log_file: str):
    global logger
    logger = logging.getLogger("BiliLike")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        logger.handlers.clear()

    formatter = logging.Formatter("%(levelname)s - %(message)s")

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    if log_enabled:
        fh = logging.FileHandler(os.path.join(SCRIPT_DIR, log_file), encoding='utf-8')
        fh.setFormatter(formatter)
        logger.addHandler(fh)

def log_info(msg):
    if logger:
        logger.info(msg)
    else:
        print(msg)

def log_warning(msg):
    if logger:
        logger.warning(msg)
    else:
        print(msg)

def log_error(msg):
    if logger:
        logger.error(msg)
    else:
        print(msg)

def log_current_time():
    time_str = time.strftime('%Y-%m-%d %H:%M:%S')
    log_info(f"⏰ 当前时间: {time_str}")

# ---------- 配置文件处理 ----------
DEFAULT_CONFIG = {
    "keywords": [
        {"word": "福瑞", "search_pages": 1, "comment_list": [], "comment_pool_mode": 0, "blacklist": [], "blacklist_mode": 1},
        {"word": "furry", "search_pages": 1, "comment_list": [], "comment_pool_mode": 0, "blacklist": [], "blacklist_mode": 1}
    ],
    "target_up": [],
    "up_blacklist": [],               # 新增：UP 主黑名单
    "blacklist": ["拍卖", "送设", "兽设"],
    "interval": 600,
    "global_search_pages": 1,
    "comment_list": ["可爱捏", "支持"],
    "like_delay": [2.0, 3.0],
    "comment_delay": [6.0, 20.0],
    "comment_pause_duration": 1200,
    "log_enabled": False,
    "log_file": "bili_like.log",
    "account": [
        {
            "cookie": "cookie.txt",
            "frequency": 1.0,
            "like_enabled": True,
            "comment_enabled": True
        }
    ]
}

def normalize_accounts(raw_accounts) -> List[Dict]:
    if not isinstance(raw_accounts, list) or len(raw_accounts) == 0:
        return [{
            "cookie": "cookie.txt",
            "frequency": 1.0,
            "like_enabled": True,
            "comment_enabled": True
        }]
    result = []
    for acc in raw_accounts:
        if isinstance(acc, str):
            acc = {"cookie": acc, "frequency": 1.0, "like_enabled": True, "comment_enabled": True}
        elif isinstance(acc, dict):
            cookie_file = acc.get("cookie", "cookie.txt")
            freq = acc.get("frequency", 1.0)
            if not isinstance(freq, (int, float)) or freq < 0:
                freq = 1.0
            like_enabled = acc.get("like_enabled", True)
            comment_enabled = acc.get("comment_enabled", True)
            acc = {
                "cookie": cookie_file,
                "frequency": freq,
                "like_enabled": like_enabled,
                "comment_enabled": comment_enabled
            }
        else:
            continue
        result.append(acc)
    if len(result) == 0:
        result = [{
            "cookie": "cookie.txt",
            "frequency": 1.0,
            "like_enabled": True,
            "comment_enabled": True
        }]
    return result

def normalize_source_item(item, type_key="word"):
    if isinstance(item, str):
        return {
            type_key: item,
            "search_pages": 0,
            "comment_list": [],
            "comment_pool_mode": 0,
            "blacklist": [],
            "blacklist_mode": 1
        }
    elif isinstance(item, dict):
        val = item.get(type_key, "")
        if not val:
            return None
        pages = item.get("search_pages", 0)
        if not isinstance(pages, int) or pages < 0:
            pages = 0
        clist = item.get("comment_list", [])
        if not isinstance(clist, list):
            clist = []
        cmode = item.get("comment_pool_mode", 0)
        if cmode not in (0, 1, 2):
            cmode = 0
        blist = item.get("blacklist", [])
        if not isinstance(blist, list):
            blist = []
        bmode = item.get("blacklist_mode", 1)
        if bmode not in (0, 1, 2):
            bmode = 1
        return {
            type_key: val,
            "search_pages": pages,
            "comment_list": clist,
            "comment_pool_mode": cmode,
            "blacklist": blist,
            "blacklist_mode": bmode
        }
    return None

def normalize_keywords(raw_keywords) -> List[Dict]:
    result = []
    if not isinstance(raw_keywords, list):
        raw_keywords = []
    for item in raw_keywords:
        normalized = normalize_source_item(item, "word")
        if normalized:
            result.append(normalized)
    return result

def normalize_target_up(raw_target_up) -> List[Dict]:
    result = []
    if not isinstance(raw_target_up, list):
        raw_target_up = []
    for item in raw_target_up:
        normalized = normalize_source_item(item, "name")
        if normalized:
            result.append(normalized)
    return result

def resolve_comment_pool(item_config: Dict, global_comment_list: List[str]) -> List[str]:
    mode = item_config.get("comment_pool_mode", 0)
    independent = item_config.get("comment_list", [])
    if mode == 0:
        return independent
    elif mode == 1:
        return independent if independent else global_comment_list
    elif mode == 2:
        combined = independent + [c for c in global_comment_list if c not in independent]
        return combined
    return independent

def resolve_blacklist(item_config: Dict, global_blacklist: List[str]) -> List[str]:
    mode = item_config.get("blacklist_mode", 1)
    independent = item_config.get("blacklist", [])
    if mode == 0:
        return independent
    elif mode == 1:
        return independent if independent else global_blacklist
    elif mode == 2:
        combined = independent + [b for b in global_blacklist if b not in independent]
        return combined
    return independent

def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        log_info(f"✅ 已生成默认配置文件: {CONFIG_FILE}")
        return DEFAULT_CONFIG.copy()

    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        config = json.load(f)

    for key, val in DEFAULT_CONFIG.items():
        if key not in config:
            config[key] = val

    config["keywords"] = normalize_keywords(config.get("keywords", []))
    config["target_up"] = normalize_target_up(config.get("target_up", []))
    config["account"] = normalize_accounts(config.get("account", []))

    # 确保 up_blacklist 是列表
    if not isinstance(config.get("up_blacklist"), list):
        config["up_blacklist"] = []
    # 确保其他列表
    if not isinstance(config.get("comment_list"), list):
        config["comment_list"] = []
    if not isinstance(config.get("blacklist"), list):
        config["blacklist"] = []

    for delay_key in ("like_delay", "comment_delay"):
        delay_val = config.get(delay_key, [2,3] if delay_key=="like_delay" else [6,20])
        if not isinstance(delay_val, list) or len(delay_val) < 2:
            delay_val = [2,3] if delay_key=="like_delay" else [6,20]
        config[delay_key] = delay_val[:2]

    if not isinstance(config.get("comment_pause_duration"), (int, float)):
        config["comment_pause_duration"] = 1200
    return config

# ---------- 已处理集合读写 ----------
def load_done_set() -> set:
    if os.path.exists(DONE_FILE):
        try:
            with open(DONE_FILE, 'r', encoding='utf-8') as f:
                return set(json.load(f))
        except:
            log_warning("读取历史记录失败，重新初始化")
    return set()

def save_done_set(done_set: set):
    with open(DONE_FILE, 'w', encoding='utf-8') as f:
        json.dump(list(done_set), f, ensure_ascii=False, indent=2)

# ---------- 多账号 Cookie 加载 ----------
def load_cookies_from_file(filename: str) -> Tuple[dict, str]:
    filepath = os.path.join(SCRIPT_DIR, filename)
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Cookie 文件不存在: {filepath}")
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            cookies = {}
            for item in line.split(';'):
                item = item.strip()
                if not item:
                    continue
                if '=' in item:
                    k, v = item.split('=', 1)
                    cookies[k] = v
            csrf = cookies.get('bili_jct', '')
            return cookies, csrf
    raise ValueError(f"未在 {filepath} 中找到有效的 Cookie 行")

_comment_pause_map: Dict[str, float] = {}

# ---------- WBI 签名 ----------
_wbi_keys_cache: Optional[Tuple[str, str]] = None
_cache_time: float = 0
CACHE_TTL = 600

def fetch_wbi_keys() -> Tuple[str, str]:
    global _wbi_keys_cache, _cache_time
    if _wbi_keys_cache and (time.time() - _cache_time) < CACHE_TTL:
        return _wbi_keys_cache

    url = "https://api.bilibili.com/x/web-interface/nav"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        wbi_img = data.get('data', {}).get('wbi_img', {})
        img_url = wbi_img.get('img_url')
        sub_url = wbi_img.get('sub_url')
        if not img_url or not sub_url:
            raise Exception("未获取到 wbi_img 数据")
        img_key = img_url.split('/')[-1].split('.')[0]
        sub_key = sub_url.split('/')[-1].split('.')[0]
        _wbi_keys_cache = (img_key, sub_key)
        _cache_time = time.time()
        return _wbi_keys_cache
    except Exception as e:
        if _wbi_keys_cache:
            log_warning(f"获取新密钥失败，使用过期缓存: {e}")
            return _wbi_keys_cache
        raise RuntimeError(f"获取 WBI 密钥失败: {e}")

def get_wbi_signed_params(params: Dict[str, str]) -> str:
    wts = int(time.time())
    sorted_params = dict(sorted(params.items()))
    query_without_wts = urllib.parse.urlencode(sorted_params)
    img_key, sub_key = fetch_wbi_keys()
    mixin_key_raw = hashlib.md5((img_key + sub_key).encode()).hexdigest()
    mixin_key = mixin_key_raw[:16]
    encoded = query_without_wts + '&' + 'wts=' + str(wts)
    tmp = []
    for i, ch in enumerate(encoded):
        tmp.append(ch)
        tmp.append(mixin_key[i % len(mixin_key)])
    w_rid = hashlib.md5(''.join(tmp).encode()).hexdigest()
    return query_without_wts + '&' + 'wts=' + str(wts) + '&' + 'w_rid=' + w_rid

# ---------- 搜索 ----------
SEARCH_ORDER = "pubdate"
SEARCH_PAGESIZE = 42
RETRY_TIMES = 2
RETRY_WAIT_RANGE = (3, 7)

def search_videos_with_retry(keyword: str, page: int = 1, order: str = "pubdate",
                             page_size: int = 42, max_retries: int = 2) -> List[dict]:
    base_url = "https://api.bilibili.com/x/web-interface/wbi/search/type"
    params = {
        "search_type": "video",
        "keyword": keyword,
        "page": page,
        "order": order,
        "page_size": page_size
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://search.bilibili.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    for attempt in range(max_retries):
        try:
            signed_query = get_wbi_signed_params(params)
            full_url = base_url + "?" + signed_query
            log_info(f"🔍 搜索: {keyword} 第{page}页")
            resp = requests.get(full_url, headers=headers, timeout=10)
            if resp.status_code == 412:
                wait = random.uniform(*RETRY_WAIT_RANGE)
                log_warning(f"搜索 '{keyword}' 触发风控 (412)，等待 {wait:.1f}s 重试...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            if data.get('code') != 0:
                log_warning(f"搜索接口返回错误: {data.get('message')}")
                return []
            results = data.get('data', {}).get('result', [])
            video_list = []
            for v in results:
                raw_title = v.get('title', '无标题')
                clean_title = re.sub(r'<em[^>]*>|</em>', '', raw_title)
                description = v.get('description', '')
                tag = v.get('tag', '')
                video_list.append({
                    'bvid': v.get('bvid', ''),
                    'aid': v.get('aid', 0),
                    'title': clean_title,
                    'description': description,
                    'tag': tag,
                    'author': v.get('author', '未知'),
                    'play': v.get('play', 0),
                    'pubdate': v.get('pubdate', 0),
                    'link': f"https://www.bilibili.com/video/{v.get('bvid', '')}" if v.get('bvid') else ''
                })
            return video_list
        except requests.exceptions.RequestException as e:
            log_error(f"请求异常 (尝试 {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(random.uniform(*RETRY_WAIT_RANGE))
        except Exception as e:
            log_error(f"未知异常 (尝试 {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(random.uniform(*RETRY_WAIT_RANGE))
    return []

# ---------- 点赞 ----------
LIKE_ACTION = 1

def do_single_like(aid: int, cookies: dict, csrf: str) -> bool:
    if not csrf:
        log_error("CSRF为空，点赞跳过")
        return False
    body = {
        'aid': str(aid),
        'like': str(LIKE_ACTION),
        'from_spmid': '333.788.recommend_more_video.-1',
        'spmid': '333.788.0.0',
        'statistics': '{"appId":100,"platform":5}',
        'eab_x': '2',
        'ramval': '1',
        'source': 'web_normal',
        'ga': '1',
        'csrf': csrf,
    }
    encoded_body = urllib.parse.urlencode(body)
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://www.bilibili.com",
        "referer": "https://www.bilibili.com/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    try:
        resp = requests.post(
            "https://api.bilibili.com/x/web-interface/archive/like",
            data=encoded_body,
            headers=headers,
            cookies=cookies,
            timeout=30
        )
        data = resp.json()
        if data.get('code') == 0:
            return True
        else:
            log_error(f"点赞失败: {data.get('message')} (code:{data.get('code')})")
            return False
    except Exception as e:
        log_error(f"点赞异常: {e}")
        return False

# ---------- 评论 ----------
def do_single_comment(aid: int, message: str, pause_duration: float,
                      cookies: dict, csrf: str, account_id: str) -> bool:
    global _comment_pause_map
    if not message or not message.strip():
        return False
    if not csrf:
        log_warning(f"[{account_id}] CSRF未找到，评论跳过")
        return False

    pause_until = _comment_pause_map.get(account_id, 0)
    if time.time() < pause_until:
        remaining = pause_until - time.time()
        log_info(f"  ⏸️ [{account_id}] 评论暂停中，剩余 {remaining:.0f} 秒，跳过本次评论")
        return False

    url = "https://api.bilibili.com/x/v2/reply/add"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
    }
    data = {
        "type": 1,
        "oid": str(aid),
        "message": message.strip(),
        "plat": 1,
        "csrf": csrf,
    }
    try:
        resp = requests.post(url, data=data, headers=headers, cookies=cookies, timeout=30)
        result = resp.json()
        code = result.get('code')
        if code == 0:
            log_info(f"  💬 [{account_id}] 评论成功 (rpid: {result.get('data', {}).get('rpid', 'N/A')})")
            return True
        elif code == 12014:
            log_warning(f"  ⚠️ [{account_id}] 评论频率限制 (code:12014)，暂停评论 {pause_duration} 秒")
            _comment_pause_map[account_id] = time.time() + pause_duration
            return "paused"
        else:
            log_error(f"  ❌ [{account_id}] 评论失败: {result.get('message')} (code:{code})")
            return False
    except Exception as e:
        log_error(f"  ❌ [{account_id}] 评论异常: {e}")
        return False

# ---------- 视频详情打印 ----------
def print_video_detail(v, hit_word=None):
    pub_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(v['pubdate'])) if v.get('pubdate') else '未知'
    desc_short = v.get('description', '')[:100] + '...' if len(v.get('description', '')) > 100 else v.get('description', '')
    tag_short = v.get('tag', '')[:100] + '...' if len(v.get('tag', '')) > 100 else v.get('tag', '')
    log_info(f"  BVID: {v['bvid']}")
    log_info(f"  标题: {v['title']}")
    log_info(f"  UP主: {v['author']}")
    log_info(f"  发布时间: {pub_time}")
    log_info(f"  简介: {desc_short}")
    log_info(f"  标签: {tag_short}")
    if hit_word:
        log_info(f"  黑名单: ❌ 命中 [{hit_word}]")
    else:
        log_info(f"  黑名单: ✅ 通过 (未命中)")

# ---------- 账号选择 ----------
def select_account(accounts: List[Dict]) -> Optional[Dict]:
    available = [acc for acc in accounts if acc["like_enabled"] or acc["comment_enabled"]]
    if not available:
        return None
    total = sum(acc["frequency"] for acc in available)
    if total == 0:
        return None
    r = random.uniform(0, total)
    cum = 0
    for acc in available:
        cum += acc["frequency"]
        if r <= cum:
            return acc
    return available[-1]

def filter_valid_accounts(accounts: List[Dict]) -> List[Dict]:
    valid = []
    for acc in accounts:
        try:
            cookies, csrf = load_cookies_from_file(acc["cookie"])
            if not acc["like_enabled"] and not acc["comment_enabled"]:
                log_warning(f"账号 {acc['cookie']} 未开启点赞或评论，将被忽略")
                continue
            acc_copy = acc.copy()
            acc_copy["_cookies"] = cookies
            acc_copy["_csrf"] = csrf
            valid.append(acc_copy)
        except Exception as e:
            log_warning(f"账号 {acc['cookie']} 无效，已跳过: {e}")
    return valid

# ---------- 主执行逻辑 ----------
def run_once(config: dict):
    log_info("\n" + "="*50)
    log_info(f"🕐 开始执行: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    log_info("="*50)

    keywords_cfg = config["keywords"]
    target_up_cfg = config["target_up"]
    global_blacklist = config.get("blacklist", [])
    up_blacklist = config.get("up_blacklist", [])          # 新增
    global_comment_list = config.get("comment_list", [])
    global_search_pages = config.get("global_search_pages", 1)
    like_delay_range = config["like_delay"]
    comment_delay_range = config["comment_delay"]
    comment_pause_duration = config.get("comment_pause_duration", 1200)
    raw_accounts = config["account"]

    valid_accounts = filter_valid_accounts(raw_accounts)
    if not valid_accounts:
        log_error("❌ 没有有效的账号可用（所有账号均加载失败或未开启点赞/评论），退出本次执行")
        return

    log_info(f"有效账号数: {len(valid_accounts)} (原始 {len(raw_accounts)} 个)")
    for acc in valid_accounts:
        log_info(f"  ✅ {acc['cookie']} (点赞:{acc['like_enabled']}, 评论:{acc['comment_enabled']})")

    if up_blacklist:
        log_info(f"UP 主黑名单: {', '.join(up_blacklist)}")

    done_set = load_done_set()
    log_info(f"已加载已处理BV数: {len(done_set)}")

    comment_pool_map = {}
    source_blacklist_map = {}
    for kw in keywords_cfg:
        word = kw["word"]
        comment_pool_map[word] = resolve_comment_pool(kw, global_comment_list)
        source_blacklist_map[word] = resolve_blacklist(kw, global_blacklist)
    for up in target_up_cfg:
        name = up["name"]
        comment_pool_map[name] = resolve_comment_pool(up, global_comment_list)
        source_blacklist_map[name] = resolve_blacklist(up, global_blacklist)

    candidates_with_source = []
    up_blacklist_skip_count = 0   # 统计因 up 黑名单跳过的数量

    # 关键词
    for kw in keywords_cfg:
        word = kw["word"]
        pages = kw["search_pages"] if kw["search_pages"] > 0 else global_search_pages
        current_blacklist = source_blacklist_map[word]
        log_info(f"\n🔍 处理关键词: {word}（搜索 {pages} 页）")
        for p in range(1, pages+1):
            videos = search_videos_with_retry(word, page=p)
            if not videos:
                log_warning(f"关键词 '{word}' 第{p}页无结果")
                continue
            log_info(f"  第{p}页获取 {len(videos)} 个视频")
            hit_list = []
            for v in videos:
                # 先检查 UP 黑名单
                if v['author'] in up_blacklist:
                    up_blacklist_skip_count += 1
                    continue
                check_text = (v['title'] + ' ' + v.get('description','') + ' ' + v.get('tag','')).lower()
                hit_word = None
                for bl_word in current_blacklist:
                    if bl_word.lower() in check_text:
                        hit_word = bl_word
                        break
                if hit_word:
                    hit_list.append((v, hit_word))
                else:
                    if v['bvid'] not in done_set and v.get('aid'):
                        candidates_with_source.append((v, word, "关键词"))
            if hit_list:
                log_info("\n📋 未通过视频详情（黑名单命中）：")
                for v, hw in hit_list:
                    print_video_detail(v, hw)

    # UP主（target_up）
    for up in target_up_cfg:
        name = up["name"]
        pages = up["search_pages"] if up["search_pages"] > 0 else global_search_pages
        current_blacklist = source_blacklist_map[name]
        log_info(f"\n🎯 处理UP主: {name}（搜索 {pages} 页，仅保留作者匹配）")
        for p in range(1, pages+1):
            videos = search_videos_with_retry(name, page=p)
            if not videos:
                log_warning(f"UP主 '{name}' 第{p}页无结果")
                continue
            filtered = [v for v in videos if v['author'] == name]
            log_info(f"  第{p}页获取 {len(videos)} 个视频，匹配作者 {len(filtered)} 个")
            hit_list = []
            for v in filtered:
                # 这里也检查 UP 黑名单（虽然 target_up 的 UP 通常不会在黑名单，但防御一下）
                if v['author'] in up_blacklist:
                    up_blacklist_skip_count += 1
                    continue
                check_text = (v['title'] + ' ' + v.get('description','') + ' ' + v.get('tag','')).lower()
                hit_word = None
                for bl_word in current_blacklist:
                    if bl_word.lower() in check_text:
                        hit_word = bl_word
                        break
                if hit_word:
                    hit_list.append((v, hit_word))
                else:
                    if v['bvid'] not in done_set and v.get('aid'):
                        candidates_with_source.append((v, name, "UP主"))
            if hit_list:
                log_info("\n📋 未通过视频详情（黑名单命中）：")
                for v, hw in hit_list:
                    print_video_detail(v, hw)

    # 去重
    seen_bvid = set()
    unique_with_source = []
    for v, src, stype in candidates_with_source:
        if v['bvid'] not in seen_bvid:
            seen_bvid.add(v['bvid'])
            unique_with_source.append((v, src, stype))

    unique = [v for v, src, stype in unique_with_source]

    log_info(f"\n📊 统计：待处理 {len(unique)} 个，UP黑名单跳过 {up_blacklist_skip_count} 个")
    if not unique:
        log_info("没有新视频需要点赞，退出本次执行")
        return

    log_info("\n📋 待处理视频详情（通过过滤）：")
    for i, v in enumerate(unique, 1):
        log_info(f"[{i}]")
        print_video_detail(v)

    log_info(f"🚀 开始处理（共 {len(unique)} 个）...")
    success_count = 0
    fail_count = 0

    for idx, (v, src, stype) in enumerate(unique_with_source, 1):
        log_current_time()
        log_info(f"\n[{idx}/{len(unique)}] 处理 {v['bvid']} (aid={v['aid']})")
        log_info(f"  标题: {v['title']} | UP: {v['author']} | 标签: {v.get('tag', '无')}")
        log_info(f"  来源: {stype} - {src}")

        acc = select_account(valid_accounts)
        if not acc:
            log_error("  ❌ 无可用账号，跳过")
            continue

        cookies = acc["_cookies"]
        csrf = acc["_csrf"]
        log_info(f"  👤 使用账号: {acc['cookie']} (点赞:{acc['like_enabled']}, 评论:{acc['comment_enabled']})")

        comment_pool = comment_pool_map.get(src, [])
        will_comment = acc["comment_enabled"] and bool(comment_pool)
        current_delay_range = comment_delay_range if will_comment else like_delay_range
        log_info(f"  延迟类型: {'评论' if will_comment else '仅点赞'} ({current_delay_range[0]}-{current_delay_range[1]}s)")

        like_ok = False
        if acc["like_enabled"]:
            like_ok = do_single_like(v['aid'], cookies, csrf)
            if like_ok:
                log_info("  ✅ 点赞成功")
                success_count += 1
            else:
                log_info("  ❌ 点赞失败")
                fail_count += 1
        else:
            log_info("  ⏭️ 点赞已禁用，跳过点赞")

        if like_ok and will_comment:
            msg = random.choice(comment_pool)
            do_single_comment(v['aid'], msg, comment_pause_duration,
                              cookies, csrf, acc["cookie"])

        done_set.add(v['bvid'])
        save_done_set(done_set)

        if idx < len(unique):
            delay = random.uniform(*current_delay_range)
            log_info(f"  ⏳ 等待 {delay:.1f}s...")
            time.sleep(delay)

    save_done_set(done_set)
    log_info(f"\n✅ 本次执行完成！已保存记录，总数: {len(done_set)}")
    log_info(f"本次成功点赞 {success_count}，失败 {fail_count}")
    log_info("="*50 + "\n")

def main_loop(interval_arg: int):
    if interval_arg:
        log_info(f"🔄 使用命令行指定的间隔: {interval_arg} 秒")
    else:
        log_info("🔄 将使用配置文件中的间隔（每轮重新读取）")
    log_info("按 Ctrl+C 可安全退出\n")

    while True:
        try:
            config = load_config()
            interval = interval_arg if interval_arg is not None else config.get("interval", DEFAULT_CONFIG["interval"])
            run_once(config)
        except KeyboardInterrupt:
            log_info("\n⚠️ 收到中断信号，正在退出...")
            break
        except Exception as e:
            log_error(f"❌ 执行过程中发生异常: {e}")
            import traceback
            traceback.print_exc()
        try:
            log_info(f"⏳ 等待 {interval} 秒后执行下一次...")
            time.sleep(interval)
        except KeyboardInterrupt:
            log_info("\n⚠️ 收到中断信号，正在退出...")
            break

if __name__ == "__main__":
    temp_config = load_config()
    log_enabled = temp_config.get("log_enabled", False)
    log_file = temp_config.get("log_file", "bili_like.log")
    setup_logging(log_enabled, log_file)

    accounts = temp_config.get("account", [])
    valid_accounts = filter_valid_accounts(accounts)
    if not valid_accounts:
        log_error("❌ 没有任何有效账号，请检查 cookie 文件及权限配置")
        input("按 Enter 键退出...")
        sys.exit(1)
    log_info(f"✅ 启动成功，发现 {len(valid_accounts)} 个有效账号")

    parser = argparse.ArgumentParser(
        description="B站自动点赞工具，支持循环执行。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "-t", "--interval",
        type=int,
        default=None,
        help="临时指定循环执行间隔时间（秒），仅本次有效，覆盖配置文件中的值"
    )
    args = parser.parse_args()

    main_loop(args.interval)
