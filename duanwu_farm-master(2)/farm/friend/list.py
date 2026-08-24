# friend.list — 好友列表（展示 id + nickname）
import requests
from config import API_HOST
from farm.auth import SignClient


def get_friends(client: SignClient) -> list:
    """
    GET /api/social/friends
    返回好友列表，每项至少含 id、nickname（兼容 nick_name）
    """
    api_path = "/api/social/friends"
    url = API_HOST + api_path
    headers = client.get_signed_headers(method="GET", api_path=api_path)
    resp = requests.get(url, headers=headers, timeout=15)
    print(f"\n[GET] friends status={resp.status_code}")
    try:
        data = resp.json()
    except Exception:
        print("GET raw:", resp.text[:500])
        raise
    if resp.status_code != 200 or not data.get("success"):
        raise Exception(f"查询好友列表失败: {data}")

    payload = data.get("data")
    if isinstance(payload, list):
        friends = payload
    elif isinstance(payload, dict):
        friends = (
            payload.get("friends")
            or payload.get("list")
            or payload.get("items")
            or payload.get("records")
            or []
        )
    else:
        friends = []
    normalized = []
    for f in friends:
        if not isinstance(f, dict):
            continue
        fid = f.get("id") or f.get("userId") or f.get("friendId")
        nick = f.get("nickname") or f.get("nick_name") or f.get("username") or "-"
        item = dict(f)
        item["id"] = fid
        item["nick_name"] = nick  # 统一展示字段
        item["nickname"] = nick
        normalized.append(item)
    return normalized


def print_friends(friends: list):
    print(f"好友数: {len(friends)}")
    for f in friends:
        print(f"  id={f.get('id')}  nick_name={f.get('nick_name')}  level={f.get('level')}")


def main():
    client = SignClient()
    client.register_signing_key(force=False)
    friends = get_friends(client)
    print_friends(friends)


if __name__ == "__main__":
    main()
