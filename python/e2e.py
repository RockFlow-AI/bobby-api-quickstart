"""Bobby 开放接口联调自测脚本。

用测试 key 按《接口文档》把已上线的接口逐项调一遍，检查返回是否符合文档，帮接入方在接入前
确认 key、网络和各条业务分支都走得通。只检查文档承诺的行为；响应里多出的字段、流里不认识的
事件一律忽略（与文档「通用约定」一致）。

覆盖：创建会话、继续对话、中断回答、查询消息、注册、查询权益、注销。

用法（Python 3.9+，依赖 httpx：pip install -r python/requirements.txt）：

    export BOBBY_API_KEY=bk_test_xxx                # 只接受测试 key
    python python/e2e.py                            # 跑全部用例，约 3～5 分钟
    python python/e2e.py --only chat,register -v    # 只跑部分用例，-v 打印每个请求
    python python/e2e.py --list                     # 列出用例

说明：

- 脚本每次运行都用一批新的用户 ID（e2e_<时间戳>_*），跑完调「注销」清掉。
- 文档里的保留测试用户（test_subscription_required、test_pro 等）会被用来发几条消息，
  不改变它们的状态，和你们自己的联调可以同时进行。
- test_trial_exhausted 注册一次后就不再返回 403。已注册过时 trial_exhausted 用例记为 SKIP；
  加 --reset-trial-user 会先注销它再测。这会影响同一把 key 下正在用这个 ID 联调的同事，请先确认。
- 回答由大模型生成，个别轮次可能要等一两分钟，属正常情况；单轮等待上限用 --timeout 调整。
- 遇到失败，把输出里的 requestId 提供给 Bobby 对接人即可排查。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

DEFAULT_BASE_URL = "https://open.bobby.ai"
PROMPT = "用一句话介绍一下苹果公司"
LONG_PROMPT = "详细分析一下特斯拉近五年的经营情况、竞争格局和估值，分点展开，越详细越好"
TRIAL_USER = "test_trial_exhausted"


class Fail(AssertionError):
    pass


class Skip(Exception):
    pass


def check(cond: Any, msg: str) -> None:
    if not cond:
        raise Fail(msg)


# ---------------------------------------------------------------------------- 客户端


@dataclass
class Turn:
    """一次发消息的结果：非 2xx 时只有 status / error；200 时是解析好的 SSE 事件。"""
    status: int
    headers: httpx.Headers
    request_id: str
    events: list[dict] = field(default_factory=list)
    error: dict | None = None
    first_text: float | None = None  # 发出请求到第一段文字的秒数
    elapsed: float = 0.0             # 发出请求到流结束的秒数

    @property
    def types(self) -> list[str]:
        return [e.get("type") for e in self.events]

    @property
    def session_id(self) -> str | None:
        return self.headers.get("x-session-id")

    @property
    def run_id(self) -> str | None:
        return self.events[0].get("runId") if self.events else None

    @property
    def text(self) -> str:
        return "".join(e.get("delta", "") for e in self.events if e.get("type") == "TEXT_MESSAGE_CONTENT")

    @property
    def terminal(self) -> dict | None:
        for e in reversed(self.events):
            if e.get("type") in ("RUN_FINISHED", "RUN_ERROR"):
                return e
        return None

    def customs(self, name: str) -> list[dict]:
        return [e.get("value") or {} for e in self.events if e.get("type") == "CUSTOM" and e.get("name") == name]


def iter_sse(resp: httpx.Response):
    """逐个产出 data: 行里的 JSON；: ping 心跳和空行跳过。"""
    for line in resp.iter_lines():
        if line.startswith("data:"):
            yield json.loads(line[5:].strip())


class Api:
    def __init__(self, base_url: str, key: str, timeout: float, verbose: bool):
        self.base = base_url.rstrip("/")
        self.key = key
        self.verbose = verbose
        self.http = httpx.Client(timeout=httpx.Timeout(30.0, read=timeout))

    def headers(self, user: str | None, key: str | None = None) -> dict[str, str]:
        h = {"Content-Type": "application/json", "X-Request-Id": f"e2e-{uuid.uuid4().hex[:16]}"}
        k = self.key if key is None else key
        if k:
            h["Authorization"] = f"Bearer {k}"
        if user is not None:
            h["X-User-Id"] = user
        return h

    def log(self, *args: Any) -> None:
        if self.verbose:
            print("      ", *args)

    def call(self, method: str, path: str, user: str | None, *, body: Any = None,
             key: str | None = None, raw: bytes | None = None) -> httpx.Response:
        h = self.headers(user, key)
        r = self.http.request(method, self.base + path, headers=h,
                              json=body if raw is None else None, content=raw)
        self.log(method, path, user, "->", r.status_code, r.text[:300], f"requestId={h['X-Request-Id']}")
        return r

    def turn(self, path: str, user: str, body: dict,
             on_event: Callable[[dict, httpx.Response], None] | None = None) -> Turn:
        """发一条消息并把流读完。on_event 在每个事件到达时回调（用来中途中断等）。"""
        h = self.headers(user)
        start = time.monotonic()
        try:
            with self.http.stream("POST", self.base + path, headers=h, json=body) as resp:
                t = Turn(resp.status_code, resp.headers, h["X-Request-Id"])
                if resp.status_code != 200:
                    resp.read()
                    t.error = error_of(resp)
                    t.elapsed = time.monotonic() - start
                    self.log("POST", path, user, "->", resp.status_code, t.error)
                    return t
                for e in iter_sse(resp):
                    t.events.append(e)
                    if t.first_text is None and e.get("type") == "TEXT_MESSAGE_CONTENT":
                        t.first_text = time.monotonic() - start
                    if on_event is not None:
                        on_event(e, resp)
        except httpx.ReadTimeout:
            raise Fail(f"等待回答超时（requestId={h['X-Request-Id']}）。大模型偶尔较慢，可加大 --timeout 后重试")
        t.elapsed = time.monotonic() - start
        first = f"{t.first_text:.1f}s" if t.first_text is not None else "-"
        self.log("POST", path, user, f"首字 {first} 总计 {t.elapsed:.1f}s",
                 f"requestId={t.request_id}", f"runId={t.run_id}")
        self.log("   ", t.types, "text:", t.text[:80])
        return t

    def create(self, user: str, prompt: str = PROMPT, mode: str | None = None, **kw) -> Turn:
        body: dict[str, Any] = {"prompt": prompt}
        if mode is not None:
            body["mode"] = mode
        return self.turn("/v1/sessions", user, body, **kw)

    def prompt(self, user: str, sid: str, prompt: str = PROMPT, **kw) -> Turn:
        return self.turn(f"/v1/sessions/{sid}/prompt", user, {"prompt": prompt}, **kw)

    def messages(self, user: str, sid: str, wait: float = 20.0) -> dict:
        """查询消息；最后一条回答还是 running 时按文档隔几秒再取，直到答完。"""
        deadline = time.monotonic() + wait
        while True:
            r = self.call("GET", f"/v1/sessions/{sid}/messages", user)
            check(r.status_code == 200, f"查询消息返回 {r.status_code}：{r.text[:200]}")
            data = r.json()
            msgs = data.get("messages") or []
            if not msgs or msgs[-1].get("status") != "running" or time.monotonic() > deadline:
                return data
            time.sleep(2)


# ---------------------------------------------------------------------------- 断言


def error_of(resp: httpx.Response) -> dict:
    try:
        return resp.json().get("error") or {}
    except ValueError:
        return {"raw": resp.text[:300]}


def expect_error(resp_or_turn: httpx.Response | Turn, statuses: int | tuple[int, ...], code: str) -> dict:
    """非 2xx 响应：HTTP 状态与 error.code 符合文档「错误」一节，并带 requestId。"""
    statuses = (statuses,) if isinstance(statuses, int) else statuses
    if isinstance(resp_or_turn, Turn):
        got, err = resp_or_turn.status, resp_or_turn.error or {}
    else:
        got, err = resp_or_turn.status_code, error_of(resp_or_turn)
    want = "/".join(map(str, statuses))
    check(got in statuses and err.get("code") == code, f"期望 {want} {code}，实际 {got} {err}")
    check(err.get("requestId"), f"错误体缺 requestId：{err}")
    return err


def expect_json(r: httpx.Response, http_status: int, /, **fields: Any) -> dict:
    """2xx 响应：状态码对，且文档列出的字段取值对；其余字段不管。"""
    check(r.status_code == http_status, f"期望 {http_status}，实际 {r.status_code}：{r.text[:200]}")
    data = r.json()
    for k, v in fields.items():
        check(data.get(k) == v, f"字段 {k} 期望 {v!r}，实际 {data.get(k)!r}（完整响应：{r.text[:200]}）")
    return data


def expect_ok_turn(t: Turn, mode: str | None = "bobby") -> dict:
    """正常答完的一轮（文档「发消息」「流式协议」）。"""
    rid = f"requestId={t.request_id}"
    check(t.status == 200, f"期望 200，实际 {t.status} {t.error}")
    check("text/event-stream" in t.headers.get("content-type", ""), f"响应不是 SSE：{t.headers.get('content-type')}（{rid}）")
    sid = t.session_id
    check(sid, f"响应头缺 X-Session-Id（{rid}）")
    check(t.events and t.events[0].get("type") == "RUN_STARTED", f"第一个事件不是 RUN_STARTED：{t.types[:3]}（{rid}）")
    check(t.events[0].get("threadId") == sid, f"RUN_STARTED.threadId 与 X-Session-Id 不一致（{rid}）")
    check(t.run_id, f"RUN_STARTED 缺 runId（{rid}）")
    term = t.terminal
    check(term is not None and term is t.events[-1], f"流没有以 RUN_FINISHED / RUN_ERROR 结束：{t.types[-3:]}（{rid}）")
    check(term["type"] == "RUN_FINISHED", f"回答以 RUN_ERROR 结束：{term.get('code')} {term.get('message')}（{rid}）")
    if mode is not None:
        got = (term.get("result") or {}).get("mode")
        check(got == mode, f"RUN_FINISHED.result.mode 期望 {mode}，实际 {got}（{rid}）")
    check(t.text.strip(), f"回答文字为空（{rid}）")
    return term


# ---------------------------------------------------------------------------- 用例


@dataclass
class Ctx:
    api: Api
    run_id: str
    reset_trial_user: bool
    created_users: set[str] = field(default_factory=set)

    def user(self, tag: str) -> str:
        u = f"e2e_{self.run_id}_{tag}"
        self.created_users.add(u)
        return u


CASES: list[tuple[str, str, str, Callable[[Ctx], None]]] = []


def case(name: str, doc: str, desc: str):
    def deco(fn):
        CASES.append((name, doc, desc, fn))
        return fn
    return deco


@case("auth", "通用约定 › 地址与鉴权、错误", "缺 key / 错 key → 401；X-User-Id 缺失或不合法 → invalid_request")
def t_auth(c: Ctx) -> None:
    api = c.api
    expect_error(api.call("GET", "/v1/users/me/entitlements", "u1", key=""), 401, "invalid_api_key")
    expect_error(api.call("GET", "/v1/users/me/entitlements", "u1", key="bk_test_" + "0" * 32), 401, "invalid_api_key")
    for bad in (None, "", "a" * 65, "a b", "a/b"):
        expect_error(api.call("GET", "/v1/users/me/entitlements", bad), (400, 422), "invalid_request")


@case("validation", "会话 › 发消息", "prompt 为空 / 空白 / 超 10000 字、mode 非法、请求体不是 JSON → invalid_request")
def t_validation(c: Ctx) -> None:
    api, u = c.api, c.user("validation")
    for body in ({}, {"prompt": ""}, {"prompt": "   "}, {"prompt": "x" * 10001}, {"prompt": "hi", "mode": "gpt"}):
        err = expect_error(api.call("POST", "/v1/sessions", u, body=body), (400, 422), "invalid_request")
        check((err.get("details") or {}).get("errors"), f"请求体 {str(body)[:40]} 的错误缺 details.errors：{err}")
    expect_error(api.call("POST", "/v1/sessions", u, raw=b"{not json"), (400, 422), "invalid_request")


@case("chat", "会话 › 发消息、流式协议、查询消息", "建会话 → 继续对话两轮 → 查询消息；别人的 / 不存在的会话 → 404")
def t_chat(c: Ctx) -> None:
    api, u = c.api, c.user("chat")
    t1 = api.create(u)
    expect_ok_turn(t1)
    sid = t1.session_id
    t2 = api.prompt(u, sid, "它的创始人是谁？")
    expect_ok_turn(t2)
    check(t2.session_id == sid, "继续对话返回的 X-Session-Id 变了")
    check(t2.run_id != t1.run_id, "两轮回答的 runId 相同")
    # 继续对话时传 mode 会被忽略
    t3 = api.turn(f"/v1/sessions/{sid}/prompt", u, {"prompt": "谢谢", "mode": "bobby_pro"})
    expect_ok_turn(t3, mode="bobby")

    data = api.messages(u, sid)
    check(data.get("sessionId") == sid, f"查询消息的 sessionId 不对：{data.get('sessionId')}")
    check(data.get("mode") == "bobby", f"查询消息的 mode 不对：{data.get('mode')}")
    msgs = data.get("messages") or []
    check([m.get("role") for m in msgs] == ["user", "assistant"] * 3, f"消息条数或顺序不对：{[m.get('role') for m in msgs]}")
    check(msgs[0].get("text") == PROMPT, f"第一条用户消息不是发出去的原文：{msgs[0].get('text')!r}")
    for turn, um, am in zip((t1, t2, t3), msgs[::2], msgs[1::2]):
        check(um.get("runId") == am.get("runId") == turn.run_id, f"消息的 runId 和流里的对不上：{um.get('runId')} / {turn.run_id}")
        check(am.get("status") == "completed", f"回答的 status 不是 completed：{am.get('status')}")
        check((am.get("text") or "").strip() == turn.text.strip(), "查询消息里的回答和流里拼出来的文字不一致")

    other = c.user("chat_other")
    expect_error(api.call("GET", f"/v1/sessions/{sid}/messages", other), 404, "session_not_found")
    expect_error(api.prompt(other, sid), 404, "session_not_found")
    expect_error(api.call("POST", f"/v1/sessions/{sid}/abort", other), 404, "session_not_found")
    expect_error(api.call("GET", "/v1/sessions/ses_does_not_exist/messages", u), 404, "session_not_found")


@case("abort", "会话 › 中断回答", "回答中再发 → 409 session_busy；中断 → aborted:true；中断后可立即再发")
def t_abort(c: Ctx) -> None:
    api, u = c.api, c.user("abort")
    state: dict[str, Any] = {}

    def on_event(e: dict, resp: httpx.Response) -> None:
        if state or e.get("type") != "TEXT_MESSAGE_CONTENT":
            return
        sid = state["sid"] = resp.headers["x-session-id"]
        state["busy"] = api.prompt(u, sid, "插一句")
        state["abort"] = api.call("POST", f"/v1/sessions/{sid}/abort", u)

    t = api.create(u, LONG_PROMPT, on_event=on_event)
    if not state:
        raise Skip("回答在出字前就结束了，没来得及中断；可重跑这个用例")
    expect_error(state["busy"], 409, "session_busy")
    r = state["abort"]
    if r.status_code == 200 and r.json().get("aborted") is False:
        raise Skip("调中断时回答刚好已经答完；可重跑这个用例")
    expect_json(r, 200, aborted=True)
    term = t.terminal
    check(term and term.get("type") == "RUN_FINISHED" and (term.get("result") or {}).get("aborted") is True,
          f"被中断的流没有以 RUN_FINISHED（result.aborted=true）结束：{term}")

    sid = state["sid"]
    expect_json(api.call("POST", f"/v1/sessions/{sid}/abort", u), 200, aborted=False)
    last = (api.messages(u, sid).get("messages") or [{}])[-1]
    check(last.get("status") == "aborted", f"被中断的回答在查询消息里 status 不是 aborted：{last.get('status')}")
    expect_ok_turn(api.prompt(u, sid, "简短点，一句话就行"))


@case("register", "用户 › 注册", "缺 email/phone 或格式错 → invalid_request；首次 created:true，重复 created:false")
def t_register(c: Ctx) -> None:
    api, u = c.api, c.user("register")
    for body in ({}, {"timezone": "UTC"}, {"phone": "13800138000"}, {"email": "not-an-email"}):
        expect_error(api.call("POST", "/v1/users/register", u, body=body), (400, 422), "invalid_request")
    expect_json(api.call("POST", "/v1/users/register", u, body={"email": "e2e@example.com", "timezone": "Asia/Shanghai"}),
                200, userId=u, status="active", created=True)
    expect_json(api.call("POST", "/v1/users/register", u, body={"phone": "+6581234567"}),
                200, userId=u, status="active", created=False)
    expect_ok_turn(api.create(u))


@case("trial_exhausted", "试用与订阅 › 试用", "test_trial_exhausted：403 trial_exhausted → 注册 → 重发成功")
def t_trial(c: Ctx) -> None:
    api, u = c.api, TRIAL_USER
    if c.reset_trial_user:
        reset_trial_user(api)
    t = api.create(u)
    if t.status == 200:
        raise Skip(f"{u} 已经注册过，不再返回 403；要重测请加 --reset-trial-user（会注销这个测试用户）")
    expect_error(t, 403, "trial_exhausted")
    expect_json(api.call("POST", "/v1/users/register", u, body={"email": "e2e@example.com"}), 200, status="active")
    expect_ok_turn(api.create(u))


@case("entitlement", "试用与订阅 › 订阅；通用约定 › 测试用户", "三个测试用户每轮回答都带 entitlement.required")
def t_entitlement(c: Ctx) -> None:
    api = c.api
    for u, feature, reason in (
        ("test_subscription_required", "bobby_pro", "subscription_required"),
        ("test_quota_exhausted", "portfolio_analysis", "quota_exhausted"),
        ("test_payment_required", "portfolio_analysis", "payment_required"),
    ):
        want = {"feature": feature, "reason": reason}
        t = api.create(u)
        expect_ok_turn(t)
        got = t.customs("entitlement.required")
        check(len(got) == 1 and {k: got[0].get(k) for k in want} == want, f"{u} 的 entitlement.required 不对：{got}")
        t2 = api.prompt(u, t.session_id, "再说一句")
        expect_ok_turn(t2)
        got2 = t2.customs("entitlement.required")
        check(len(got2) == 1 and {k: got2[0].get(k) for k in want} == want, f"{u} 第二轮没带 entitlement.required：{got2}")
        events = api.messages(u, t.session_id)["messages"][1].get("events") or []
        check(any(e.get("name") == "entitlement.required" for e in events), f"{u} 查询消息的 events 里没有 entitlement.required")


@case("downgrade", "会话 › 发消息", "未订阅用户传 mode=bobby_pro：降级为 bobby，RUN_STARTED 后紧跟 entitlement.required")
def t_downgrade(c: Ctx) -> None:
    api, u = c.api, c.user("downgrade")
    t = api.create(u, mode="bobby_pro")
    expect_ok_turn(t, mode="bobby")
    second = t.events[1]
    check(second.get("type") == "CUSTOM" and second.get("name") == "entitlement.required",
          f"RUN_STARTED 之后的第一个事件不是 entitlement.required：{second}")
    value = second.get("value") or {}
    check(value.get("feature") == "bobby_pro" and value.get("reason") == "subscription_required", f"事件内容不对：{value}")
    check(api.messages(u, t.session_id).get("mode") == "bobby", "查询消息的 mode 不是 bobby")


@case("entitlements", "试用与订阅 › 订阅", "查询权益：普通用户没有订阅；test_pro 有 bobby_pro")
def t_entitlements(c: Ctx) -> None:
    api = c.api
    data = expect_json(api.call("GET", "/v1/users/me/entitlements", c.user("ent")), 200, subscriptions=[])
    check(isinstance(data.get("quotas"), list), f"quotas 不是数组：{data.get('quotas')!r}")
    data = expect_json(api.call("GET", "/v1/users/me/entitlements", "test_pro"), 200)
    subs = [s for s in data.get("subscriptions") or [] if s.get("product") == "bobby_pro"]
    check(subs and subs[0].get("expiresAt"), f"test_pro 的 subscriptions 里没有带 expiresAt 的 bobby_pro：{data}")


@case("pro", "试用与订阅 › 订阅（联调：模拟已订阅）", "test_pro：不传 mode 走 bobby_pro，可继续对话；传 bobby 走 bobby")
def t_pro(c: Ctx) -> None:
    api, u = c.api, "test_pro"
    t = api.create(u)
    expect_ok_turn(t, mode="bobby_pro")
    check(not t.customs("entitlement.required"), "已订阅用户不应收到 entitlement.required")
    expect_ok_turn(api.prompt(u, t.session_id, "再简单补充一句"), mode="bobby_pro")
    data = api.messages(u, t.session_id)
    check(data.get("mode") == "bobby_pro", f"Pro 会话查询消息的 mode 不是 bobby_pro：{data.get('mode')}")
    check(len(data.get("messages") or []) == 4, f"Pro 会话应有 4 条消息，实际 {len(data.get('messages') or [])}")
    expect_ok_turn(api.create(u, mode="bobby"), mode="bobby")


@case("delete", "用户 › 注销；通用约定 › 错误", "注销 → 202；删除完成前会话接口返回 404 user_not_found")
def t_delete(c: Ctx) -> None:
    api, u = c.api, c.user("delete")
    expect_ok_turn(api.create(u))
    expect_json(api.call("DELETE", "/v1/users/me", u), 202)
    # 删除在后台进行：删完之前会话接口返回 404 user_not_found，删完之后同一个 ID 就是新的试用用户
    t = api.create(u)
    if t.status != 200:
        expect_error(t, 404, "user_not_found")


# ---------------------------------------------------------------------------- 清理


def reset_trial_user(api: Api, timeout: float = 120.0) -> None:
    """注销 test_trial_exhausted 并等删完：删除中建会话返回 404，删完后返回 403。"""
    r = api.call("DELETE", "/v1/users/me", TRIAL_USER)
    check(r.status_code in (202, 404), f"注销 {TRIAL_USER}：{r.status_code} {r.text[:200]}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        t = api.create(TRIAL_USER)
        if t.status in (200, 403):
            return
        time.sleep(3)
    raise Skip(f"{TRIAL_USER} 注销后 {timeout:.0f}s 内还没删完，稍后再试")


def cleanup(c: Ctx) -> None:
    for u in sorted(c.created_users):
        try:
            c.api.call("DELETE", "/v1/users/me", u)
        except httpx.HTTPError:
            pass


# ---------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default=os.getenv("BOBBY_BASE_URL", DEFAULT_BASE_URL))
    ap.add_argument("--only", help="逗号分隔的用例名，见 --list")
    ap.add_argument("--list", action="store_true", help="列出用例")
    ap.add_argument("--timeout", type=float, default=300.0, help="单轮回答的等待上限（秒），默认 300")
    ap.add_argument("--reset-trial-user", action="store_true",
                    help=f"先注销 {TRIAL_USER} 再测 trial_exhausted（会影响同一把 key 下用它联调的同事）")
    ap.add_argument("--keep", action="store_true", help="跑完不注销本次创建的 e2e_* 用户（便于事后查看）")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印每个请求的摘要、耗时和 requestId")
    args = ap.parse_args()

    if args.list:
        for name, doc, desc, _ in CASES:
            print(f"{name:16} {desc}\n{'':16} 文档：{doc}")
        return 0

    key = os.getenv("BOBBY_API_KEY", "")
    if not key.startswith("bk_test_"):
        print("请把 BOBBY_API_KEY 设为测试 key（bk_test_ 开头）。本脚本不接受正式 key", file=sys.stderr)
        return 2

    selected = CASES
    if args.only:
        names = {n.strip() for n in args.only.split(",") if n.strip()}
        unknown = names - {n for n, *_ in CASES}
        if unknown:
            print(f"未知用例：{', '.join(sorted(unknown))}（见 --list）", file=sys.stderr)
            return 2
        selected = [x for x in CASES if x[0] in names]

    api = Api(args.base_url, key, args.timeout, args.verbose)
    try:
        probe = api.call("GET", "/v1/users/me/entitlements", "e2e_probe")
    except httpx.HTTPError as e:
        print(f"连不上 {api.base}：{e}", file=sys.stderr)
        return 2
    print(f"目标 {api.base}  key {key[:12]}…")
    if probe.status_code == 401:
        print("API key 无效或已吊销，请联系 Bobby 对接人", file=sys.stderr)
        return 2

    c = Ctx(api, time.strftime("%m%d%H%M%S"), args.reset_trial_user)
    results: list[tuple[str, str, str, str]] = []
    for name, doc, desc, fn in selected:
        print(f"\n▶ {name}  {desc}")
        start = time.monotonic()
        try:
            fn(c)
            status, msg = "PASS", ""
        except Skip as e:
            status, msg = "SKIP", str(e)
        except Fail as e:
            status, msg = "FAIL", str(e)
        except Exception as e:     # 网络错误、JSON 解析失败等，同样记失败，继续下一个用例
            status, msg = "FAIL", f"{type(e).__name__}: {e}"
            if args.verbose:
                traceback.print_exc()
        print(f"  {status}  {time.monotonic() - start:.1f}s  {msg}")
        results.append((name, doc, status, msg))

    if not args.keep:
        cleanup(c)

    count = {s: sum(r[2] == s for r in results) for s in ("PASS", "FAIL", "SKIP")}
    print(f"\n通过 {count['PASS']}，失败 {count['FAIL']}，跳过 {count['SKIP']}")
    for name, doc, status, msg in results:
        if status != "PASS":
            print(f"  {status} {name}（文档：{doc}）：{msg}")
    return 1 if count["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
