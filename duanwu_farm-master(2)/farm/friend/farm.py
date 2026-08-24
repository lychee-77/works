# friend.farm — GET 好友农场地块
import requests
from config import API_HOST, FRIEND_ID
from farm.auth import SignClient


def get_friend_farm(client: SignClient, friend_id: str = None) -> dict:
    """查询好友农场，返回完整 JSON（含 data.slots）"""
    friend_id = friend_id or FRIEND_ID
    api_path = f"/api/farm/friend/{friend_id}"
    url = API_HOST + api_path
    headers = client.get_signed_headers(method="GET", api_path=api_path)
    resp = requests.get(url, headers=headers, timeout=15)
    print(f"\n[GET] friend={friend_id} status={resp.status_code}")
    try:
        data = resp.json()
    except Exception:
        print("GET raw:", resp.text[:500])
        raise
    if resp.status_code != 200 or not data.get("success"):
        raise Exception(f"查询好友农场失败: {data}")
    payload = data.get("data")
    if isinstance(payload, dict):
        from farm.own.farm import enrich_farm_payload
        enrich_farm_payload(payload)
    return data


def main():
    client = SignClient()
    client.register_signing_key(force=False)
    data = get_friend_farm(client, FRIEND_ID)
    slots = data.get("data", {}).get("slots") or []
    print(f"地块数: {len(slots)}")
    for s in slots:
        print(
            f"  slot={s.get('slotIndex')} crop={s.get('cropName')} "
            f"state={s.get('state')} rem={s.get('remainingMinutes')} "
            f"stolen={s.get('alreadyStolenByVisitor')} full={s.get('stolenFull')} "
            f"protected={s.get('isProtected')}"
        )


if __name__ == "__main__":
    main()
