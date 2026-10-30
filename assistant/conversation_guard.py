"""Verify the active chat by its dedicated title label, not by arbitrary matching text.

Title locator reference:
https://github.com/Hello-Mr-Crab/pywechat/blob/main/src/pyweixin/Uielements.py
CurrentChatNameText uses a dotted AutomationId ending in current_chat_name_label.
"""
from conversation_targets import search_display_name


def selected_name_matches(name, expected):
    return search_display_name(name) == expected


TITLE_ID = ('content_view.top_content_view.title_h_view.left_v_view.left_content_v_view.'
            'left_ui_.big_title_line_h_view.current_chat_name_label')


def _title_candidates(root):
    """Native UIA conditions avoid Python walking every nested Qt layout container."""
    from wx4py.core import uiautomation as uia
    client = uia._AutomationClient.instance().IUIAutomation
    prop = uia.PropertyId
    condition = client.CreateAndCondition(
        client.CreatePropertyCondition(prop.ControlTypeProperty, uia.ControlType.TextControl),
        client.CreatePropertyCondition(prop.IsOffscreenProperty, False))
    ids = client.CreateOrCondition(
        client.CreatePropertyCondition(prop.AutomationIdProperty, TITLE_ID),
        client.CreatePropertyCondition(prop.AutomationIdProperty, 'current_chat_name_label'))
    elements = root.Element.FindAll(4, client.CreateAndCondition(condition, ids))  # Descendants
    if not elements or elements.Length == 0:
        # Some builds prepend MainView to the ID. Read IDs only, never message text.
        elements = root.Element.FindAll(4, condition)
    if not elements:
        return []
    if elements.Length > 4096:
        raise RuntimeError('当前窗口可见文本控件过多，无法完成标题核对，未发送。')
    result = []
    for index in range(elements.Length):
        element = elements.GetElement(index)
        if (element.CurrentAutomationId or '').split('.')[-1] == 'current_chat_name_label':
            result.append(uia.Control.CreateControlFromElement(element))
    return result


def chat_titles(root):
    """Validate only dedicated title candidates and their ancestors, not the whole tree."""
    from wx4py.core import uiautomation as uia
    titles = []
    for candidate in _title_candidates(root):
        if candidate.IsOffscreen:
            continue
        parent = candidate.GetParentControl()
        for _ in range(128):
            if parent is None:
                break
            if uia.ControlsAreSame(parent, root):
                titles.append(candidate.Name or '')
                break
            if (parent.ControlTypeName in {'ListControl', 'ListItemControl', 'DataItemControl',
                                          'EditControl', 'WindowControl'}
                    or 'Search' in (parent.ClassName or '')):
                break
            parent = parent.GetParentControl()
        else:
            raise RuntimeError('会话标题的父级路径异常，无法完成核对，未发送。')
    return titles


def verify_current_chat(root, target):
    from wx4py.core import uiautomation as uia
    from wx4py.features.messaging.listener import _find_session_list
    titles = chat_titles(root)
    if titles:
        if len(titles) == 1 and titles[0].strip() == target.name:
            return
        raise RuntimeError(f'当前会话标题核对失败，未发送（标题控件={len(titles)}；'
                           f'名称匹配={sum(title.strip() == target.name for title in titles)}）。'
                           '请勿在发送期间手动切换会话。')

    session_list = _find_session_list(root)
    selected, rows, supported = [], 0, 0
    if session_list:
        # Ask the container first; Qt can expose selection here without item patterns.
        try:
            pattern = session_list.GetSelectionPattern()
            selected = list(pattern.GetSelection()) if pattern else []
        except Exception:
            selected = []
        if not selected:
            for control, _ in uia.WalkControl(session_list, includeTop=False, maxDepth=6):
                if (control.ControlTypeName not in {'ListItemControl', 'DataItemControl'}
                        and control.ClassName != 'mmui::ChatSessionCell'):
                    continue
                rows += 1
                state = None
                try:
                    pattern = control.GetPattern(uia.PatternId.SelectionItemPattern)
                    if pattern:
                        state = pattern.IsSelected
                except Exception:
                    pass
                if state is None:
                    try:
                        legacy = control.GetLegacyIAccessiblePattern()
                        if legacy:
                            # STATE_SYSTEM_SELECTED is distinct from focused or selectable.
                            state = bool(legacy.State & uia.AccessibleState.Selected)
                    except Exception:
                        pass
                if state is not None:
                    supported += 1
                if state is True:
                    selected.append(control)
    names = [control.Name or '' for control in selected]
    if len(names) == 1 and selected_name_matches(names[0], target.name):
        return
    raise RuntimeError('无法核对当前会话，未发送（标题控件=0；'
                       f'会话行={rows}；可读选中状态={supported}；已选={len(names)}；'
                       f'名称匹配={sum(selected_name_matches(name, target.name) for name in names)}）。'
                       '请反馈这行完整提示。')
