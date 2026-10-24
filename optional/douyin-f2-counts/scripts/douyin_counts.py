"""Read public Douyin post counts with F2 and a local, user-owned session."""

import argparse
import asyncio
import csv
import datetime as dt
import html
import json
import logging
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse, quote

F2_COMMIT = "f6be8c0ffba9a127075bbeafe4838716650b6325"
TZ = dt.timezone(dt.timedelta(hours=8))


def now():
    return dt.datetime.now(TZ).isoformat(timespec="seconds")


def load_accounts(path):
    text = path.read_text(encoding="utf-8-sig")
    values = json.loads(text) if path.suffix.lower() == ".json" else text.splitlines()
    if not isinstance(values, list):
        raise ValueError("Input must be a JSON array or one Douyin handle/profile URL per line")
    result, seen = [], set()
    for value in values:
        if isinstance(value, str):
            if not value.strip():continue
            value = {"url": value.strip()}
        if not isinstance(value, dict):raise ValueError("Each item needs a handle or profile URL")
        raw = value.get("url") or value.get("account")
        if not isinstance(raw, str):raise ValueError("Keep Douyin handles as strings, including leading zeros")
        raw = raw.strip()
        expected = value.get("account")
        if expected is not None and not isinstance(expected, str):raise ValueError("Optional account must be a string")
        uid = None
        if re.fullmatch(r"MS4w[A-Za-z0-9_-]+", raw):uid = raw
        elif re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", raw):expected = raw
        else:
            parsed = urlparse(raw)
            match = re.fullmatch(r"/user/(MS4w[A-Za-z0-9_-]+)/?", parsed.path)
            if parsed.scheme != "https" or parsed.netloc not in {"www.douyin.com", "douyin.com"} or not match:
                raise ValueError("Use a Douyin handle or a full official profile URL")
            uid = match[1]
        key = ("uid", uid) if uid else ("handle", expected)
        if key not in seen:
            seen.add(key)
            result.append({"sec_uid": uid, "url": "https://www.douyin.com/user/" + uid if uid else "", "expected_account": expected})
    if not result:raise ValueError("No accounts supplied")
    return result


class SearchUnavailable(Exception):pass
class SearchNeedsVerification(Exception):pass


def search_identity(data, expected):
    """Only exact handle matches in normal search responses qualify."""
    found=set();stack=[data];visited=0
    while stack and visited<20000:
        item=stack.pop();visited+=1
        if isinstance(item, dict):
            handle=item.get("unique_id") or item.get("short_id")
            uid=item.get("sec_uid")
            if handle is not None and str(handle)==expected and isinstance(uid,str) and re.fullmatch(r"MS4w[A-Za-z0-9_-]+",uid):found.add(uid)
            stack.extend(value for value in item.values() if isinstance(value,(dict,list)))
        elif isinstance(item,list):stack.extend(item)
    return found


async def resolve_handle(page, expected, interactive=False):
    found=set();tasks=set();verification={"needed":False}
    async def response_data(response):
        parsed=urlparse(response.url)
        if parsed.hostname not in {"www.douyin.com","douyin.com"} or "search" not in parsed.path:return
        try:
            payload=await response.json()
            if (payload.get('search_nil_info') or {}).get('search_nil_type')=='verify_check':verification['needed']=True
            identities=search_identity(payload,expected)
            if identities:verification['needed']=False
            found.update(identities)
        except Exception:pass
    def observe(response):
        task=asyncio.create_task(response_data(response));tasks.add(task);task.add_done_callback(tasks.discard)
    page.on("response",observe)
    try:
        await page.goto("https://www.douyin.com/search/"+quote(expected,safe="")+"?type=user",wait_until="domcontentloaded",timeout=40000)
        try:
            await page.wait_for_function("""() => /抖音号[：:]|安全验证|拖动滑块|请完成.{0,8}验证|暂无搜索结果|没有找到相关/.test(document.body?.innerText||'')""",timeout=15000)
        except Exception:pass
        if tasks:await asyncio.wait_for(asyncio.gather(*list(tasks),return_exceptions=True),timeout=5)
        text=await page.locator('body').inner_text(timeout=5000)
        blocked=(verification['needed'] and not found) or await page.locator('iframe[src*="captcha"]:visible,iframe[src*="verifycenter"]:visible').count() or re.search(r'安全验证|拖动滑块|请完成.{0,8}验证',text)
        if blocked:
            if not interactive:raise SearchNeedsVerification()
            print("搜索接口要求安全验证（verify_check）。窗口已保留；请本人在窗口完成验证并正常搜索目标抖音号，看到该账号结果后回到终端按回车。",flush=True)
            verification['needed']=False
            await asyncio.to_thread(input,"处理完成后按回车继续：")
            if tasks:await asyncio.wait_for(asyncio.gather(*list(tasks),return_exceptions=True),timeout=5)
            text=await page.locator('body').inner_text(timeout=5000)
            if (verification['needed'] and not found) or await page.locator('iframe[src*="captcha"]:visible,iframe[src*="verifycenter"]:visible').count() or re.search(r'安全验证|拖动滑块|请完成.{0,8}验证',text):raise SearchNeedsVerification()
        candidates=await page.locator('a[href*="/user/"]').evaluate_all("""links => {const rows=[];for(const a of links){if(!a.getClientRects().length)continue;let p=a;for(let i=0;i<5&&p;i++,p=p.parentElement){const text=p.innerText||'';if(text.length>1600)break;const match=text.match(/抖音号[：:]?\\s*([^\\s]+)/);if(match){rows.push({url:a.href,handle:match[1]});break;}}}return rows;}""")
        for row in candidates:
            parsed=urlparse(row['url']);match=re.fullmatch(r'/user/(MS4w[A-Za-z0-9_-]+)/?',parsed.path)
            if row['handle']==expected and parsed.scheme=='https' and parsed.hostname in {'www.douyin.com','douyin.com'} and match:found.add(match[1])
        if tasks:await asyncio.wait_for(asyncio.gather(*list(tasks),return_exceptions=True),timeout=5)
        if len(found)>1:raise IdentityMismatch()
        if not found:raise SearchUnavailable()
        uid=next(iter(found))
        return dict(sec_uid=uid,url='https://www.douyin.com/user/'+uid,expected_account=expected)
    finally:
        page.remove_listener('response',observe)
        for task in list(tasks):task.cancel()


class LoginRequired(Exception):
    pass


class IdentityMismatch(Exception):
    pass


class ProfileUnavailable(Exception):
    pass


def parse_user(data, item):
    user = data.get("user") or {}
    account = user.get("unique_id") or user.get("short_id")
    count = user.get("aweme_count")
    if user.get("secret") == 1 or not account or type(count) is not int or count < 0:
        raise ProfileUnavailable()
    if user.get("sec_uid") != item["sec_uid"] or (item["expected_account"] and str(account) != item["expected_account"]):
        raise IdentityMismatch()
    return {"account": str(account), "name": user.get("nickname") or "", "count": count}


def csv_safe(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def write_results(folder, report):
    folder.mkdir(parents=True, exist_ok=True)
    rows = report["results"]
    report["summary"] = {s: sum(r["status"] == s for r in rows) for s in ("success", "failed", "skipped")}
    target = folder / "results.json"
    temporary = folder / "results.tmp"
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    columns = ["account", "name", "count", "url", "status", "error", "observed_at"]
    with (folder / "results.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        writer.writerows([[csv_safe(r.get(c, "")) for c in columns] for r in rows])
    rendered = []
    for row in rows:
        name = html.escape(row.get("name") or row.get("account") or "主页")
        link = html.escape(row["url"], quote=True)
        cells = [f'<a href="{link}" target="_blank" rel="noopener noreferrer">{name}</a>']
        cells += [html.escape(str(row.get(k, "") if row.get(k) is not None else "")) for k in ("account", "count", "status", "error", "observed_at")]
        rendered.append("<tr>" + "".join("<td>" + c + "</td>" for c in cells) + "</tr>")
    body = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>抖音作品数量</title><style>body{font:16px sans-serif;margin:32px}table{border-collapse:collapse}th,td{padding:10px;border:1px solid #ccc}a{color:#1264b7}</style><h1>抖音作品数量</h1><p>查询时间均为北京时间。点击昵称打开主页；失败数量留空。</p><table><thead><tr><th>昵称／主页</th><th>抖音号</th><th>作品数</th><th>状态</th><th>错误</th><th>查询时间</th></tr></thead><tbody>'
    (folder / "results.html").write_text(body + "".join(rendered) + "</tbody></table></html>", encoding="utf-8")


async def run(args):
    from playwright.async_api import async_playwright

    items = load_accounts(args.input) if args.command == "query" else []
    args.profile = args.profile.expanduser().resolve()
    if args.command == "query" and not args.profile.is_dir():
        raise ValueError("Profile does not exist; run login first")
    async with async_playwright() as playwright:
        options = {"headless": args.command != "login" and not (getattr(args,"headed",False) or getattr(args,"interactive",False)), "locale": "zh-CN", "viewport": {"width": 1440, "height": 1000}}
        if args.browser_executable:
            options["executable_path"] = str(args.browser_executable.expanduser().resolve())
        context = await playwright.chromium.launch_persistent_context(str(args.profile), **options)
        try:
            page = await context.new_page()
            if args.command == "login":
                try:
                    await page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=40000)
                except Exception:
                    print("Page load timed out; finish loading in the browser.", flush=True)
                await asyncio.to_thread(input, "在独立窗口正常登录并手动完成验证，然后回到此处按回车：")
                cookies = await context.cookies(["https://www.douyin.com/"])
                ok = any(c["name"] in {"sessionid", "sessionid_ss"} and c["value"] for c in cookies)
                print("Login session saved." if ok else "No login session detected; run login again.")
                return 0 if ok else 2

            # Suppress upstream debug logs: signed URLs and session values are not report data.
            logging.disable(logging.CRITICAL)
            from f2.apps.douyin.crawler import DouyinCrawler
            from f2.apps.douyin.model import UserProfile
            from f2.apps.douyin.utils import ClientConfManager

            folder = args.output.expanduser().resolve() / (dt.datetime.now(TZ).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
            report = {"started_at": now(), "source": "F2 user.aweme_count", "f2_commit_expected": F2_COMMIT, "results": []}
            jar = await context.cookies(["https://www.douyin.com/"])
            cookies = {c["name"]: c["value"] for c in jar}
            logged_in = bool(cookies.get("sessionid") or cookies.get("sessionid_ss"))
            headers = ClientConfManager.headers().copy()
            headers["User-Agent"] = await page.evaluate("navigator.userAgent")
            cfg = {"cookie": "; ".join(k + "=" + v for k, v in cookies.items()), "headers": headers, "timeout": args.timeout, "max_retries": 1, "max_tasks": 1}
            streak = 0
            for index, item in enumerate(items):
                row = {"url": item["url"], "account": item["expected_account"] or "", "count": None, "observed_at": now()}
                if streak >= args.max_failures:
                    row.update(status="skipped", error="ConsecutiveFailureLimit")
                else:
                    if index:
                        await asyncio.sleep(args.interval)
                    try:
                        if not logged_in:
                            raise LoginRequired()
                        if not item["sec_uid"]:
                            item=await resolve_handle(page,item["expected_account"],getattr(args,"interactive",False))
                            row["url"]=item["url"]
                        # Manual verification can refresh session tokens. Read only this dedicated context.
                        jar=await context.cookies(["https://www.douyin.com/"])
                        cookies={c["name"]:c["value"] for c in jar}
                        cfg["cookie"]="; ".join(k+"="+v for k,v in cookies.items())
                        params = UserProfile(sec_user_id=item["sec_uid"], msToken=cookies.get("msToken", ""))
                        async with DouyinCrawler(cfg) as client:
                            data = await asyncio.wait_for(client.fetch_user_profile(params), timeout=args.timeout * 3)
                        row.update(parse_user(data, item), status="success", error="")
                        streak = 0
                    except Exception as exc:
                        if isinstance(exc,(SearchNeedsVerification,SearchUnavailable)):
                            await page.screenshot(path=str(folder/"search-diagnostic.png"))
                            text=await page.locator('body').inner_text(timeout=5000)
                            indicators=re.findall(r'.{0,20}(?:验证码|安全验证|拖动滑块|抖音号).{0,35}',text)
                            (folder/"search-diagnostic.json").write_text(json.dumps(dict(title=await page.title(),indicators=indicators[:12]),ensure_ascii=False,indent=2),encoding='utf-8')
                        row.update(status="failed", error=type(exc).__name__)
                        streak += 1
                row["observed_at"] = now()
                report["results"].append(row)
                write_results(folder, report)
                print(json.dumps({k: row.get(k) for k in ("account", "count", "status", "error")}, ensure_ascii=True), flush=True)
            report["finished_at"] = now()
            write_results(folder, report)
            print("Report: " + str(folder / "results.html"), flush=True)
            return 0 if all(r["status"] == "success" for r in report["results"]) else 2
        finally:
            await context.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("login", "query"):
        command = sub.add_parser(name)
        command.add_argument("--profile", type=Path, required=True, help="Dedicated local browser profile; do not share")
        command.add_argument("--browser-executable", type=Path)
        if name == "query":
            command.add_argument("--headed", action="store_true", help="Show the dedicated search browser")
            command.add_argument("--interactive", action="store_true", help="Keep the browser open for manual verification, then resume")
        if name == "query":
            command.add_argument("--input", type=Path, required=True)
            command.add_argument("--output", type=Path, required=True)
            command.add_argument("--interval", type=float, default=3)
            command.add_argument("--timeout", type=float, default=20)
            command.add_argument("--max-failures", type=int, default=3)
    args = parser.parse_args()
    if args.command == "query" and (args.interval < 0 or args.timeout <= 0 or args.max_failures < 1):
        parser.error("Require interval >= 0, timeout > 0 and max-failures >= 1")
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("Interrupted; any already written results are retained.", file=sys.stderr)
        return 130
    except Exception as exc:
        # Never print upstream exceptions, which can contain signed URLs or credentials.
        message = str(exc) if isinstance(exc, (ValueError, FileNotFoundError)) else "Check setup, browser availability and profile lock"
        print(type(exc).__name__ + ": " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
