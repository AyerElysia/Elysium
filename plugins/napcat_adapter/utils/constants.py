"""常量定义：QQ 表情映射、消息类型枚举、命令类型。"""

from __future__ import annotations

from enum import Enum


# ------------------------------------------------------------------
# 命令类型（向后兼容旧枚举 + 新增）
# ------------------------------------------------------------------


class CommandType(Enum):
    """适配器命令类型枚举。"""

    # --- 保留原有命令 ---
    GROUP_BAN = "set_group_ban"
    GROUP_WHOLE_BAN = "set_group_whole_ban"
    GROUP_KICK = "set_group_kick"
    SEND_POKE = "send_poke"
    DELETE_MSG = "delete_msg"
    AI_VOICE_SEND = "ai_voice_send"
    SET_EMOJI_LIKE = "set_msg_emoji_like"
    SEND_AT_MESSAGE = "send_at_message"
    SEND_LIKE = "send_like"

    # --- 账号命令 ---
    SET_PROFILE = "set_qq_profile"
    SET_AVATAR = "set_qq_avatar"
    SET_LONGNICK = "set_self_longnick"
    SET_ONLINE_STATUS = "set_online_status"
    SET_INPUT_STATUS = "set_input_status"

    # --- 消息命令 ---
    MARK_READ = "mark_msg_as_read"
    MARK_ALL_READ = "_mark_all_as_read"
    GET_MSG_HISTORY = "get_group_msg_history"
    GET_FRIEND_MSG_HISTORY = "get_friend_msg_history"
    SEND_FORWARD_MSG = "send_forward_msg"
    FORWARD_SINGLE_MSG = "forward_friend_single_msg"
    GET_RECENT_CONTACT = "get_recent_contact"

    # --- 群管理命令 ---
    SET_ADMIN = "set_group_admin"
    SET_GROUP_CARD = "set_group_card"
    SET_GROUP_NAME = "set_group_name"
    SET_GROUP_LEAVE = "set_group_leave"
    SET_SPECIAL_TITLE = "set_group_special_title"
    SET_GROUP_PORTRAIT = "set_group_portrait"
    GROUP_SIGN = "set_group_sign"
    SEND_GROUP_NOTICE = "_send_group_notice"
    GET_GROUP_NOTICE = "_get_group_notice"
    DEL_GROUP_NOTICE = "_del_group_notice"
    SET_ESSENCE = "set_essence_msg"
    DELETE_ESSENCE = "delete_essence_msg"
    GET_ESSENCE_LIST = "get_essence_msg_list"
    GET_GROUP_MEMBERS = "get_group_member_list"
    GET_GROUP_HONOR = "get_group_honor_info"
    GET_GROUP_SHUT_LIST = "get_group_shut_list"

    # --- 好友命令 ---
    DELETE_FRIEND = "delete_friend"
    GET_FRIEND_LIST = "get_friend_list"
    FRIEND_POKE = "friend_poke"

    # --- 文件命令 ---
    UPLOAD_GROUP_FILE = "upload_group_file"
    UPLOAD_PRIVATE_FILE = "upload_private_file"
    GET_GROUP_FILES = "get_group_root_files"
    DELETE_GROUP_FILE = "delete_group_file"
    CREATE_FOLDER = "create_group_file_folder"

    # --- 社交命令 ---
    ARK_SHARE = "ArkSharePeer"
    GET_AI_CHARACTERS = "get_ai_characters"

    # --- 系统命令 ---
    GET_STATUS = "get_status"
    GET_VERSION_INFO = "get_version_info"
    CLEAN_CACHE = "clean_cache"
    OCR_IMAGE = "ocr_image"

    # --- 通用透传 ---
    RAW_API = "raw_api"

    def __str__(self) -> str:
        return self.value


# ------------------------------------------------------------------
# 消息类型
# ------------------------------------------------------------------


class RealMessageType:
    """OneBot 消息段类型。"""

    text = "text"
    face = "face"
    image = "image"
    record = "record"
    video = "video"
    at = "at"
    rps = "rps"
    dice = "dice"
    shake = "shake"
    poke = "poke"
    share = "share"
    reply = "reply"
    forward = "forward"
    node = "node"
    json = "json"
    file = "file"


class NoticeType:
    """通知事件类型。"""

    friend_add = "friend_add"
    friend_recall = "friend_recall"
    group_recall = "group_recall"
    group_admin = "group_admin"
    group_ban = "group_ban"
    group_card = "group_card"
    group_decrease = "group_decrease"
    group_increase = "group_increase"
    group_upload = "group_upload"
    group_msg_emoji_like = "group_msg_emoji_like"
    essence = "essence"
    notify = "notify"
    bot_offline = "bot_offline"

    class Notify:
        poke = "poke"
        input_status = "input_status"
        title = "title"
        profile_like = "profile_like"

    class GroupBan:
        ban = "ban"
        lift_ban = "lift_ban"

    class GroupAdmin:
        set = "set"
        unset = "unset"

    class GroupDecrease:
        leave = "leave"
        kick = "kick"
        kick_me = "kick_me"

    class GroupIncrease:
        approve = "approve"
        invite = "invite"

    class Essence:
        add = "add"
        delete = "delete"


class RequestType:
    """请求事件类型。"""

    friend = "friend"
    group = "group"

    class Group:
        add = "add"
        invite = "invite"


# 支持的消息格式（出站）
ACCEPT_FORMAT = [
    "text",
    "image",
    "emoji",
    "reply",
    "voice",
    "command",
    "voiceurl",
    "music",
    "videourl",
    "file",
    "forward",
    "face",
    "json",
    "share",
]

# ------------------------------------------------------------------
# QQ 表情映射表
# ------------------------------------------------------------------

QQ_FACE: dict[str, str] = {
    "0": "[表情：惊讶]", "1": "[表情：撇嘴]", "2": "[表情：色]", "3": "[表情：发呆]",
    "4": "[表情：得意]", "5": "[表情：流泪]", "6": "[表情：害羞]", "7": "[表情：闭嘴]",
    "8": "[表情：睡]", "9": "[表情：大哭]", "10": "[表情：尴尬]", "11": "[表情：发怒]",
    "12": "[表情：调皮]", "13": "[表情：呲牙]", "14": "[表情：微笑]", "15": "[表情：难过]",
    "16": "[表情：酷]", "18": "[表情：抓狂]", "19": "[表情：吐]", "20": "[表情：偷笑]",
    "21": "[表情：可爱]", "22": "[表情：白眼]", "23": "[表情：傲慢]", "24": "[表情：饥饿]",
    "25": "[表情：困]", "26": "[表情：惊恐]", "27": "[表情：流汗]", "28": "[表情：憨笑]",
    "29": "[表情：悠闲]", "30": "[表情：奋斗]", "31": "[表情：咒骂]", "32": "[表情：疑问]",
    "33": "[表情：嘘]", "34": "[表情：晕]", "35": "[表情：折磨]", "36": "[表情：衰]",
    "37": "[表情：骷髅]", "38": "[表情：敲打]", "39": "[表情：再见]", "41": "[表情：发抖]",
    "42": "[表情：爱情]", "43": "[表情：跳跳]", "46": "[表情：猪头]", "49": "[表情：拥抱]",
    "53": "[表情：蛋糕]", "56": "[表情：刀]", "59": "[表情：便便]", "60": "[表情：咖啡]",
    "63": "[表情：玫瑰]", "64": "[表情：凋谢]", "66": "[表情：爱心]", "67": "[表情：心碎]",
    "74": "[表情：太阳]", "75": "[表情：月亮]", "76": "[表情：赞]", "77": "[表情：踩]",
    "78": "[表情：握手]", "79": "[表情：胜利]", "85": "[表情：飞吻]", "86": "[表情：怄火]",
    "89": "[表情：西瓜]", "96": "[表情：冷汗]", "97": "[表情：擦汗]", "98": "[表情：抠鼻]",
    "99": "[表情：鼓掌]", "100": "[表情：糗大了]", "101": "[表情：坏笑]", "102": "[表情：左哼哼]",
    "103": "[表情：右哼哼]", "104": "[表情：哈欠]", "105": "[表情：鄙视]", "106": "[表情：委屈]",
    "107": "[表情：快哭了]", "108": "[表情：阴险]", "109": "[表情：左亲亲]", "110": "[表情：吓]",
    "111": "[表情：可怜]", "112": "[表情：菜刀]", "114": "[表情：篮球]", "116": "[表情：示爱]",
    "118": "[表情：抱拳]", "119": "[表情：勾引]", "120": "[表情：拳头]", "121": "[表情：差劲]",
    "123": "[表情：NO]", "124": "[表情：OK]", "125": "[表情：转圈]", "129": "[表情：挥手]",
    "137": "[表情：鞭炮]", "144": "[表情：喝彩]", "146": "[表情：爆筋]", "147": "[表情：棒棒糖]",
    "169": "[表情：手枪]", "171": "[表情：茶]", "172": "[表情：眨眼睛]", "173": "[表情：泪奔]",
    "174": "[表情：无奈]", "175": "[表情：卖萌]", "176": "[表情：小纠结]", "177": "[表情：喷血]",
    "178": "[表情：斜眼笑]", "179": "[表情：doge]", "181": "[表情：戳一戳]", "182": "[表情：笑哭]",
    "183": "[表情：我最美]", "185": "[表情：羊驼]", "187": "[表情：幽灵]", "201": "[表情：点赞]",
    "212": "[表情：托腮]", "262": "[表情：脑阔疼]", "263": "[表情：沧桑]", "264": "[表情：捂脸]",
    "265": "[表情：辣眼睛]", "266": "[表情：哦哟]", "267": "[表情：头秃]", "268": "[表情：问号脸]",
    "269": "[表情：暗中观察]", "270": "[表情：emm]", "271": "[表情：吃瓜]", "272": "[表情：呵呵哒]",
    "273": "[表情：我酸了]", "277": "[表情：滑稽狗头]", "281": "[表情：翻白眼]", "282": "[表情：敬礼]",
    "283": "[表情：狂笑]", "284": "[表情：面无表情]", "285": "[表情：摸鱼]", "286": "[表情：魔鬼笑]",
    "287": "[表情：哦]", "289": "[表情：睁眼]", "293": "[表情：摸锦鲤]", "294": "[表情：期待]",
    "295": "[表情：拿到红包]", "297": "[表情：拜谢]", "298": "[表情：元宝]", "299": "[表情：牛啊]",
    "300": "[表情：胖三斤]", "302": "[表情：左拜年]", "303": "[表情：右拜年]", "305": "[表情：右亲亲]",
    "306": "[表情：牛气冲天]", "307": "[表情：喵喵]", "311": "[表情：打call]", "312": "[表情：变形]",
    "314": "[表情：仔细分析]", "317": "[表情：菜汪]", "318": "[表情：崇拜]", "319": "[表情：比心]",
    "320": "[表情：庆祝]", "323": "[表情：嫌弃]", "324": "[表情：吃糖]", "325": "[表情：惊吓]",
    "326": "[表情：生气]", "332": "[表情：举牌牌]", "333": "[表情：烟花]", "334": "[表情：虎虎生威]",
    "336": "[表情：豹富]", "337": "[表情：花朵脸]", "338": "[表情：我想开了]", "339": "[表情：舔屏]",
    "341": "[表情：打招呼]", "342": "[表情：酸Q]", "343": "[表情：我方了]", "344": "[表情：大怨种]",
    "345": "[表情：红包多多]", "346": "[表情：你真棒棒]", "347": "[表情：大展宏兔]", "349": "[表情：坚强]",
    "350": "[表情：贴贴]", "351": "[表情：敲敲]", "352": "[表情：咦]", "353": "[表情：拜托]",
    "354": "[表情：尊嘟假嘟]", "355": "[表情：耶]", "356": "[表情：666]", "357": "[表情：裂开]",
    "392": "[表情：龙年快乐]", "393": "[表情：新年中龙]", "394": "[表情：新年大龙]",
    "395": "[表情：略略略]", "396": "[表情：龙年快乐]", "424": "[表情：按钮]",
}
