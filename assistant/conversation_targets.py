"""Resolve local contact records without fuzzy names or cross-conversation identities."""
from dataclasses import dataclass
import re
from html import unescape


SYSTEM_CONTACTS = {'filehelper', 'weixin', 'fmessage', 'newsapp', 'medianote', 'floatbottle',
                   'qqmail', 'tmessage', 'qmessage', 'qqsync', 'readerapp', 'brandsessionholder',
                   'notifymessage', 'notification_messages', 'opencustomerservicemsg'}


@dataclass(frozen=True)
class ChatTarget:
    key: str
    username: str
    name: str
    kind: str


def is_friend(row, self_id):
    username = row['username']
    return (isinstance(username, str) and bool(username) and username != self_id
            and '@' not in username and not username.startswith('gh_') and username not in SYSTEM_CONTACTS
            and isinstance(row['flag'], int) and bool(row['flag'] & 1)
            and row['delete_flag'] == 0 and row['verify_flag'] == 0)


def friend_targets(conn, self_id):
    required = {'username', 'nick_name', 'remark', 'flag', 'delete_flag', 'verify_flag'}
    columns = {row[1] for row in conn.execute('PRAGMA table_info(contact)')}
    if not required <= columns:
        raise RuntimeError('当前联系人库缺少好友识别字段，已停止；不会把群成员或公众号当作好友回复。')
    result = {}
    for row in conn.execute('SELECT username,nick_name,remark,flag,delete_flag,verify_flag FROM contact'):
        if is_friend(row, self_id):
            name = row['remark'] or row['nick_name'] or row['username']
            key = 'private:' + row['username']
            result[key] = ChatTarget(key, row['username'], name, 'contact')
    return result


def verify_target(conn, target, self_id):
    """Validate current identity and reject ambiguous display names before UI search."""
    columns = {row[1] for row in conn.execute('PRAGMA table_info(contact)')}
    if 'remark' not in columns:
        raise RuntimeError('联系人信息缺少备注字段，无法核对发送目标。')
    rows = conn.execute('SELECT username,nick_name,remark FROM contact').fetchall()
    matches = [row for row in rows if target.name in {row['nick_name'], row['remark'], row['username']}]
    if len({row['username'] for row in matches}) != 1 or matches[0]['username'] != target.username:
        raise RuntimeError('发送名称不唯一或已变化；请在微信为同名好友设置唯一备注，再重新检查。')
    if target.kind == 'contact':
        current = friend_targets(conn, self_id).get(target.key)
        if current != target:
            raise RuntimeError('好友关系或备注已经变化，已阻止发送，请重新检查。')
    elif not target.username.endswith('@chatroom'):
        raise RuntimeError('群聊身份核对失败，已阻止发送。')


def search_display_name(value):
    """Only normalize known presentation markup; never substring-match a target."""
    if not isinstance(value, str):
        return ''
    # Qt rich-text highlighting may be exposed in an accessible name. Keep unknown tags.
    if re.search(r'</(?:font|span|em|b|strong)>', value, re.I):
        value = re.sub(r'</?(?:font|span|em|b|strong)\b[^>]*>', '', value, flags=re.I)
        value = unescape(value)
    lines = value.strip().splitlines()
    return lines[0].strip() if lines else ''


def exact_search_result(results, target):
    # Caller must first verify unique name + identity against the contact database.
    category = '群聊' if target.kind == 'group' else '联系人'
    matches = [item for item in results.get(category, []) if search_display_name(item.name) == target.name]
    if not matches:
        matches = [item for item in results.get('最常使用', []) if search_display_name(item.name) == target.name]
    if len(matches) != 1:
        counts = '，'.join(f'{key}={len(results.get(key, []))}'
                          for key in ('最常使用', category, '聊天记录'))
        raise RuntimeError(f'微信搜索未取得唯一的名称完整匹配，未发送（{counts}；完整匹配={len(matches)}）。'
                           '聊天记录不作为发送目标；请反馈这行完整提示。')
    return matches[0]
