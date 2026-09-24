"""事件订阅 Intents 位标记。

intents 是一个位图，需要订阅哪类事件就把对应的位置为 1，
最终把需要的位做按位或后传给 Identify。

官方文档：https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html

注意：单聊与群聊事件共用同一个位 ``GROUP_AND_C2C_EVENT (1<<25)``，
平台没有提供「只订阅单聊」的位，因此默认订阅里这一位是必须的，
公域频道事件 ``PUBLIC_GUILD_MESSAGES (1<<30)`` 默认不订阅。
"""

from __future__ import annotations

from enum import IntFlag


class Intents(IntFlag):
    """所有可订阅事件类型。"""

    # --- 基础事件（默认有权限） ---
    GUILDS = 1 << 0
    GUILD_MEMBERS = 1 << 1
    #: 公域频道 @机器人 消息，基础事件（本项目默认不订阅）
    PUBLIC_GUILD_MESSAGES = 1 << 30

    # --- 需要申请权限的事件 ---
    #: 私域频道全量消息，仅私域机器人可用
    GUILD_MESSAGES = 1 << 9
    GUILD_MESSAGE_REACTIONS = 1 << 10
    DIRECT_MESSAGE = 1 << 12
    #: 单聊 + 群聊全部事件（C2C_MESSAGE_CREATE / GROUP_AT_MESSAGE_CREATE / FRIEND_ADD ...）
    GROUP_AND_C2C_EVENT = 1 << 25
    INTERACTION = 1 << 26
    MESSAGE_AUDIT = 1 << 27
    FORUMS_EVENT = 1 << 28
    AUDIO_ACTION = 1 << 29

    #: 默认订阅：单聊 + 群聊事件 = 33554432。
    #: 单聊与群聊共用这一位，无法只订阅单聊；不含公域频道事件(1<<30)。
    DEFAULT = GROUP_AND_C2C_EVENT


#: 事件名 -> 所属 intent，便于排查「订阅了但收不到」
EVENT_INTENTS: dict[str, Intents] = {
    "GUILD_CREATE": Intents.GUILDS,
    "GUILD_UPDATE": Intents.GUILDS,
    "GUILD_DELETE": Intents.GUILDS,
    "CHANNEL_CREATE": Intents.GUILDS,
    "CHANNEL_UPDATE": Intents.GUILDS,
    "CHANNEL_DELETE": Intents.GUILDS,
    "GUILD_MEMBER_ADD": Intents.GUILD_MEMBERS,
    "GUILD_MEMBER_UPDATE": Intents.GUILD_MEMBERS,
    "GUILD_MEMBER_REMOVE": Intents.GUILD_MEMBERS,
    "MESSAGE_CREATE": Intents.GUILD_MESSAGES,
    "AT_MESSAGE_CREATE": Intents.PUBLIC_GUILD_MESSAGES,
    "PUBLIC_MESSAGE_DELETE": Intents.PUBLIC_GUILD_MESSAGES,
    "DIRECT_MESSAGE_CREATE": Intents.DIRECT_MESSAGE,
    "C2C_MESSAGE_CREATE": Intents.GROUP_AND_C2C_EVENT,
    "GROUP_AT_MESSAGE_CREATE": Intents.GROUP_AND_C2C_EVENT,
    "GROUP_MESSAGE_CREATE": Intents.GROUP_AND_C2C_EVENT,
    "FRIEND_ADD": Intents.GROUP_AND_C2C_EVENT,
    "FRIEND_DEL": Intents.GROUP_AND_C2C_EVENT,
    "GROUP_ADD_ROBOT": Intents.GROUP_AND_C2C_EVENT,
    "GROUP_DEL_ROBOT": Intents.GROUP_AND_C2C_EVENT,
    "INTERACTION_CREATE": Intents.INTERACTION,
}

#: 单聊（C2C）相关事件，业务侧只需关注这些
C2C_EVENTS = frozenset(
    {
        "C2C_MESSAGE_CREATE",  # 用户单聊发消息
        "FRIEND_ADD",  # 用户添加机器人
        "FRIEND_DEL",  # 用户删除机器人
        "C2C_MSG_RECEIVE",  # 用户开启主动消息推送
        "C2C_MSG_REJECT",  # 用户关闭主动消息推送
    }
)


def describe(intents: int) -> str:
    """把 intents 数值翻译成可读的事件名列表，方便日志排查 4013/4014。"""
    names = [i.name for i in Intents if i.value and (intents & i.value) == i.value]
    return ", ".join(names) if names else "0"
