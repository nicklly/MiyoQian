# -*- coding: utf-8 -*-
"""扫码登录与凭证刷新。"""

from __future__ import annotations

import json
import pathlib
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from .. import constants as c
from ..core import cookies, crypto
from ..core.http import ApiClient


class QRLogin:
    def __init__(self, client: ApiClient, device_id: str, device_fp: str) -> None:
        self.client = client
        self.device_id = device_id
        self.device_fp = device_fp

    def fetch(self) -> tuple[str, str]:
        data = self.client.post_json(
            c.QRCODE_FETCH_URL,
            json={"app_id": c.QRCODE_APP_ID, "device": self.device_id},
        )
        ensure_ok(data, "生成二维码失败")
        url = str(data["data"]["url"])
        ticket = parse_qs(urlparse(url).query).get("ticket", [""])[0]
        if not ticket:
            raise RuntimeError("二维码接口未返回 ticket")
        return url, ticket

    def wait(self, ticket: str, timeout: int = 120) -> dict[str, str]:
        started = time.time()
        last_status = ""
        while time.time() - started < timeout:
            data = self.client.post_json(
                c.QRCODE_QUERY_URL,
                json={"app_id": c.QRCODE_APP_ID, "device": self.device_id, "ticket": ticket},
            )
            ensure_ok(data, "查询二维码状态失败")
            status = str(data.get("data", {}).get("stat", ""))
            if status != last_status:
                if status == "Init":
                    print("等待扫码...")
                elif status == "Scanned":
                    print("已扫码，请在米游社 APP 确认登录。")
                elif status == "Confirmed":
                    print("已确认，正在换取凭证。")
                last_status = status
            if status == "Confirmed":
                raw = data.get("data", {}).get("payload", {}).get("raw", "{}")
                payload = json.loads(raw)
                uid = str(payload.get("uid") or "")
                game_token = str(payload.get("token") or "")
                if not uid or not game_token:
                    raise RuntimeError("扫码结果缺少 uid/game_token")
                return {"uid": uid, "game_token": game_token}
            time.sleep(2)
        raise TimeoutError("扫码登录超时")

    def exchange_tokens(self, uid: str, game_token: str) -> dict[str, str]:
        body = {"account_id": int(uid), "game_token": game_token}
        data = self.client.post_json(
            c.TOKEN_BY_GAME_TOKEN_URL,
            json=body,
            headers={
                "x-rpc-app_id": c.PASSPORT_APP_ID,
                "x-rpc-client_type": "2",
                "x-rpc-game_biz": "bbs_cn",
                "x-rpc-device_id": self.device_id,
                "x-rpc-device_fp": self.device_fp,
                "ds": crypto.ds_k2(body),
                "user-agent": c.QR_MOBILE_UA,
                "content-type": "application/json",
            },
        )
        ensure_ok(data, "game_token 换 stoken 失败")
        token_info = data.get("data", {}).get("token", {})
        user_info = data.get("data", {}).get("user_info", {})
        stoken = str(token_info.get("token") or "")
        mid = str(user_info.get("mid") or "")
        stuid = str(user_info.get("aid") or uid)
        if not stoken or not mid:
            raise RuntimeError("接口未返回 stoken/mid")
        ltoken = self._get_ltoken(stoken, mid)
        cookie_token = self._get_cookie_token(stoken, mid)
        cookie = cookies.build_cookie(stuid, mid, ltoken, cookie_token)
        return {
            "stuid": stuid,
            "stoken": stoken,
            "mid": mid,
            "cookie": cookie,
        }

    def _passport_headers(self, stoken: str, mid: str) -> dict[str, str]:
        return {
            "user-agent": c.QR_MOBILE_UA,
            "x-rpc-app_version": c.QR_LOGIN_VERSION,
            "x-rpc-client_type": "5",
            "x-requested-with": "com.mihoyo.hyperion",
            "referer": "https://webstatic.mihoyo.com",
            "x-rpc-device_id": self.device_id,
            "x-rpc-device_fp": self.device_fp,
            "cookie": f"mid={mid};stoken={stoken}",
        }

    def _get_ltoken(self, stoken: str, mid: str) -> str:
        headers = self._passport_headers(stoken, mid)
        headers["ds"] = crypto.ds_x4(query=f"stoken={stoken}")
        data = self.client.get_json(
            c.LTOKEN_BY_STOKEN_URL,
            headers=headers,
            params={"stoken": stoken},
        )
        ensure_ok(data, "stoken 换 ltoken 失败")
        return str(data.get("data", {}).get("ltoken") or "")

    def _get_cookie_token(self, stoken: str, mid: str) -> str:
        headers = self._passport_headers(stoken, mid)
        headers["ds"] = crypto.ds_x4(query=f"stoken={stoken}")
        data = self.client.get_json(
            c.COOKIE_TOKEN_BY_STOKEN_URL,
            headers=headers,
            params={"stoken": stoken},
        )
        ensure_ok(data, "stoken 换 cookie_token 失败")
        return str(data.get("data", {}).get("cookie_token") or "")


def print_qrcode(text: str, image_path: pathlib.Path | None = None) -> None:
    import qrcode

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=1,
        border=1,
    )
    qr.add_data(text)
    qr.make(fit=True)
    qr.print_ascii(invert=True)
    if image_path:
        qrcode.make(text).save(image_path)


def refresh_cookie_token(client: ApiClient, account: dict[str, Any]) -> bool:
    try:
        data = client.get_json(
            c.COOKIE_TOKEN_REFRESH_URL,
            headers={"cookie": cookies.stoken_cookie(account), "user-agent": c.DEFAULT_MOBILE_UA},
        )
        ensure_ok(data, "刷新 cookie_token 失败")
    except Exception:
        return False
    token = str(data.get("data", {}).get("cookie_token") or "")
    if not token:
        return False
    account["cookie"] = cookies.replace_or_append_cookie_value(
        str(account.get("cookie") or ""), "cookie_token", token
    )
    return True


def ensure_ok(data: dict[str, Any], message: str) -> None:
    if data.get("retcode") != 0:
        raise RuntimeError(f"{message}: retcode={data.get('retcode')} message={data.get('message')}")
