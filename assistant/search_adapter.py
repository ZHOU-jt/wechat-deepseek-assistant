"""Read the search popup only, retaining section boundaries and exact identity checks."""
import time
from types import SimpleNamespace

from conversation_targets import exact_search_result, search_display_name

SECTIONS = {'最常使用', '联系人', '群聊', '聊天记录', '功能', '搜索网络结果'}


def _prop(control, name):
    value = getattr(control, name, '')
    return value if isinstance(value, str) else ''


def _children(control):
    return control.GetChildren() or []


def _row_name(control):
    name = _prop(control, 'Name')
    if name.strip():
        return name
    # Some Qt cells expose the display name on a child instead of on the row.
    def texts(node, depth):
        if depth > 3:
            return []
        text = _prop(node, 'Name')
        if text.strip():
            return [text]
        result = []
        for child in _children(node):
            result.extend(texts(child, depth + 1))
        return result
    return '\n'.join(texts(control, 0))


def parse_search_rows(items):
    """Keep rows opaque: text inside history cards must never become a chat target."""
    groups, section = {}, None
    visited = 0

    def visit(control, depth):
        nonlocal section, visited
        visited += 1
        if visited > 300 or depth > 7:
            raise RuntimeError('微信搜索控件结构超出读取范围，未发送。')
        cls, auto = _prop(control, 'ClassName'), _prop(control, 'AutomationId')
        kind = _prop(control, 'ControlTypeName')
        row = ('SearchContentCellView' in cls or cls == 'mmui::XTableCell'
               or kind == 'ListItemControl')
        raw_name = _row_name(control) if row else _prop(control, 'Name')
        name = search_display_name(raw_name)
        # Search result named "群聊" is still a result when it has a result ID/class.
        result_cell = 'SearchContentCellView' in cls or auto.startswith('search_item_')
        if name in SECTIONS and not result_cell:
            section = name
            groups.setdefault(section, [])
            return
        if row:
            if '查看全部' in name:
                return
            if auto.startswith('search_item_function'):
                section = '功能'
                return
            if section in SECTIONS and name:
                groups.setdefault(section, []).append(SimpleNamespace(
                    name=raw_name, ctrl=control, group=section, auto_id=auto))
            return
        children = _children(control)
        if not children and name:
            # Unknown headers stop inheritance from a previous eligible section.
            section = None
            return
        for child in children:
            visit(child, depth + 1)

    for item in items:
        visit(item, 0)
    return groups


def read_popup(chat):
    popup = chat._get_search_popup()
    if not popup:
        return {}, '搜索面板未读到'
    # Resolve list structurally, allowing layout wrappers; never scan the desktop.
    queue = [(popup, 0)]
    lists = []
    visited = 0
    while queue:
        node, depth = queue.pop(0)
        visited += 1
        if visited > 300:
            raise RuntimeError('微信搜索面板过大，未发送。')
        if _prop(node, 'ControlTypeName') == 'ListControl':
            lists.append(node)
            continue
        if depth < 6:
            queue.extend((child, depth + 1) for child in _children(node))
    if len(lists) != 1:
        return {}, f'搜索列表数量={len(lists)}'
    groups = parse_search_rows(_children(lists[0]))
    return groups, '搜索列表已读取'


def search_target(chat, target):
    if not chat._input_search(target.name):
        raise RuntimeError('微信搜索框未能写入目标名称，未发送。')
    # Search UI updates asynchronously. Re-read only; never retype, click, or send on retry.
    last_error = None
    for attempt in range(4):
        if attempt:
            time.sleep(0.4)
        groups, stage = read_popup(chat)
        try:
            return exact_search_result(groups, target)
        except RuntimeError as error:
            last_error = str(error) + '读取阶段：' + stage + '。'
    raise RuntimeError(last_error)
