"""用户画像：按 user_id 记录昵称、生日、喜好、备注等，支持 LLM 自动提取与提示词注入。

存于 data/user_profiles.json：
  { "10001": {"nickname": "小明", "birthday": "05-20", "likes": [...], "notes": [...], "updated_at": ts} }
"""
import json
import time
from pathlib import Path
from typing import Optional

from .llm_helpers import generate_json_reply, RoleContext

DEFAULT_EXTRACT_PROMPT = (
    "你是用户画像提取助手。任务：从用户与虚拟角色（AI助手/语音角色）的这段对话中，"
    "只提取关于【对话中用户本人】的长期稳定信息，绝不能把角色信息当成用户信息。\n"
    "铁律：\n"
    "1. nickname 只记录「用户希望被怎么称呼」或「用户自称、角色常称呼用户的昵称」；"
    "用户对角色说的称呼、给角色起的外号（如角色名、丛雨、主人、AI）以及角色自称（如本座、吾辈）"
    "都不是用户昵称，禁止收录。\n"
    "2. likes/dislikes 只记录用户本人的喜好与厌恶；角色在回复里提到的角色自己的喜好与厌恶"
    "（如角色喜欢甜食、害怕幽灵）一律忽略，不得写入。\n"
    "3. birthday、notes 只记录用户本人明确说出的生日与重要安排。\n"
    "4. 拿不准就不存，宁可少存不可错存；没有可提取内容时输出空对象。\n"
    "5. 若用户明确表示旧画像信息已过时或改口（如“我现在不喜欢猫了”“别再说我爱吃辣”），"
    "请输出对应移除字段（likes_remove / dislikes_remove / notes_remove 数组列出要删的旧条目），"
    "整类清空可用 clear_likes=true 等，程序会据此替换或删除旧内容。\n"
    "只输出JSON，格式："
    '{"nickname": "称呼/昵称(可选)", "birthday": "MM-DD(可选)", "likes": ["用户本人喜好"], '
    '"dislikes": ["用户本人厌恶"], "notes": ["重要事项"], '
    '"likes_remove": ["已过时的旧喜好"], "notes_remove": ["已过时备注"]}，'
    "不需要的字段可省略，禁止输出任何其它文字。"
)


def _strip_assistant_leak(data: dict, user_text: str, reply_text: str) -> dict:
    """把模型误从角色回复里照抄的内容从用户画像中剔除。"""
    if not isinstance(data, dict):
        return data
    user_s = str(user_text or "").strip()
    reply_s = str(reply_text or "").strip()

    def _in_user(text) -> bool:
        return str(text or "").strip() and str(text) in user_s

    def _reply_copied(text) -> bool:
        item = str(text or "").strip()
        if not item or not reply_s:
            return False
        if item in reply_s:
            return True
        for run in range(len(item), 2, -1):
            for i in range(0, len(item) - run + 1):
                if item[i:i + run] in reply_s:
                    return True
        return False

    out = dict(data)
    nick = str(out.get("nickname", "") or "").strip()
    if nick and not _in_user(nick) and _reply_copied(nick):
        out.pop("nickname", None)
    for key in ("likes", "dislikes", "notes"):
        items = out.get(key)
        if not isinstance(items, list):
            continue
        kept = [i for i in items
                if _in_user(i) or not _reply_copied(str(i or "").strip())]
        if kept:
            out[key] = kept
        else:
            out.pop(key, None)
    return out


class UserProfileManager:
    def __init__(self, config, data_path: Path):
        self.config = config
        self.data_path = Path(data_path)
        self.data_path.mkdir(parents=True, exist_ok=True)
        self.file = self.data_path / "user_profiles.json"
        self.profiles = {}
        self._dirty = False
        self.load()

    def load(self):
        from .jsonio import load_json
        self.profiles = load_json(self.file, {})
        if not isinstance(self.profiles, dict):
            self.profiles = {}

    def save(self):
        try:
            from .jsonio import save_json
            save_json(self.file, self.profiles)
        except Exception as e:
            print(f"保存用户画像失败: {e}")

    def get(self, user_id: str) -> dict:
        return dict(self.profiles.get(str(user_id), {}))

    def update(self, user_id: str, data: dict, replace: bool = False):
        uid = str(user_id)
        if not uid or not isinstance(data, dict):
            return
        profile = self.profiles.setdefault(uid, {})
        if replace:
            for key in ("nickname", "birthday"):
                val = str(data.get(key, "") or "").strip()
                if val:
                    profile[key] = val
                else:
                    profile.pop(key, None)
            for key in ("likes", "dislikes", "notes"):
                items = data.get(key)
                if isinstance(items, list):
                    cleaned = [str(i).strip() for i in items if str(i).strip()]
                    if cleaned:
                        profile[key] = cleaned[:50]
                    else:
                        profile.pop(key, None)
            profile["updated_at"] = time.time()
            self.save()
            return
        for key in ("nickname", "birthday"):
            if key not in data:
                continue
            val = str(data.get(key, "") or "").strip()
            if val:
                profile[key] = val
        for key in ("likes", "dislikes", "notes"):
            remove = data.get(key + "_remove")
            if isinstance(remove, list):
                bucket = profile.get(key, [])
                for item in remove:
                    item = str(item).strip()
                    if item:
                        bucket = [x for x in bucket if x != item]
                # 纯删除不做截断：已存的条目超过上限时，删一条不该顺带丢掉最老的几条
                profile[key] = bucket
            if data.get("clear_" + key):
                profile.pop(key, None)
        for key in ("likes", "dislikes", "notes"):
            items = data.get(key)
            if isinstance(items, list):
                bucket = profile.setdefault(key, [])
                for item in items:
                    item = str(item).strip()
                    if item and item not in bucket:
                        bucket.append(item)
                profile[key] = bucket[-50:]
                if not profile[key]:
                    profile.pop(key, None)
        profile["updated_at"] = time.time()
        self.save()

    def delete(self, user_id: str) -> bool:
        uid = str(user_id)
        if uid in self.profiles:
            del self.profiles[uid]
            self.save()
            return True
        return False

    # ---------------- LLM 自动提取 ----------------
    async def extract_from_dialog(self, ctx: RoleContext, user_text: str, reply_text: str,
                                  user_id: str):
        """对话后异步提取用户信息（失败静默）。"""
        if not str(user_text or "").strip():
            return
        prompt = str(self.config.get("profiles_extract_prompt", "") or DEFAULT_EXTRACT_PROMPT)
        system = (
            f"{prompt}\n当前用户ID: {user_id}\n已有画像(供去重参考): "
            f"{json.dumps(self.get(user_id), ensure_ascii=False)}"
        )
        user_prompt = (f"用户说：{user_text}\n"
                       f"角色回复：{reply_text}\n"
                       "依据只允许来自【用户说】的内容；【角色回复】中角色的自称、喜好、转述、"
                       "客套等一律不得写进用户画像。")
        try:
            data = await generate_json_reply(ctx, system, user_prompt, max_tokens=256)
        except Exception as e:
            print(f"用户画像提取失败: {e}")
            return
        if not isinstance(data, dict):
            return
        self.update(user_id, _strip_assistant_leak(data, user_text, reply_text))

    # ---------------- 注入提示词 ----------------
    def build_injection(self, user_id: str) -> str:
        if not self.config.get("profiles_enabled", False):
            return ""
        profile = self.profiles.get(str(user_id))
        if not profile:
            return ""
        template = str(self.config.get("profiles_inject_template", "") or
                       "【用户画像】关于当前用户的已知信息：{profile}")
        lines = []
        if profile.get("nickname"):
            lines.append(f"昵称: {profile['nickname']}")
        if profile.get("birthday"):
            lines.append(f"生日: {profile['birthday']}")
        for key, label in (("likes", "喜好"), ("dislikes", "厌恶"), ("notes", "重要事项")):
            items = profile.get(key) or []
            if items:
                lines.append(f"{label}: " + "、".join(str(i) for i in items[:10]))
        if not lines:
            return ""
        text = "\n".join(lines)
        try:
            max_chars = int(self.config.get("profiles_max_chars", 300))
        except (TypeError, ValueError):
            max_chars = 300
        return template.replace("{profile}", text[:max_chars])
