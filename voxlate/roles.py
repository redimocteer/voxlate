"""Project-local speaker assignments; display names never identify audio caches."""
import re
from .common import VoxlateError

MAX_ROLES = 50
MAX_NAME = 8


def apply_automatic_voice_mode(project, role_count):
    """Suggest per-sentence voices only from uniform mode, once per project."""
    if (role_count <= 5 or project.get('voice_mode', 'uniform') != 'uniform'
            or project.get('many_roles_voice_suggested', False)):
        return False
    project['many_roles_voice_suggested'] = True
    project['voice_mode'] = 'individual'
    return True


def automatic_role_name(index):
    letters = ''
    number = index + 1
    while number:
        number, remainder = divmod(number-1, 26)
        letters = chr(65+remainder) + letters
    return '角色'+letters


def validate_roles(project, *, allow_legacy_names=False):
    roles = project.get('roles', [])
    if not isinstance(roles, list) or not 1 <= len(roles) <= MAX_ROLES:
        raise VoxlateError(f'请保留 1～{MAX_ROLES} 个角色。')
    ids, names = set(), set()
    sentence_ids = {s['id'] for s in project.get('segments', [])}
    for role in roles:
        if not isinstance(role, dict):
            raise VoxlateError('角色数据格式无效。')
        ident, name = role.get('id', ''), role.get('name', '')
        if not isinstance(ident, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}', ident) or ident in ids:
            raise VoxlateError('角色编号无效或重复。')
        if (not isinstance(name, str) or name != name.strip() or not 1 <= len(name) <= (12 if allow_legacy_names else MAX_NAME)
                or any(ord(c) < 32 or c in '\\/:*?"<>|' for c in name)):
            raise VoxlateError('角色名须为 1～8 个字，不能含换行或 \\/:*?"<>|。')
        if name.casefold() in names:
            raise VoxlateError('角色名不能重复。')
        reference = role.get('reference_sentence_id')
        if reference is not None and (type(reference) is not int or reference not in sentence_ids):
            raise VoxlateError(f'「{name}」的参考句不存在。')
        ids.add(ident)
        names.add(name.casefold())
    if any(s.get('role_id') not in ids for s in project.get('segments', [])):
        raise VoxlateError('部分句子尚未分配有效角色。')


def ensure_roles(project):
    if not project.get('roles'):
        project['roles'] = [dict(id='role_a', name='角色A', reference_sentence_id=None)]
    if not isinstance(project['roles'], list) or len(project['roles']) > MAX_ROLES or any(
            not isinstance(r, dict) or not isinstance(r.get('name'), str) or not isinstance(r.get('id'), str)
            for r in project['roles']):
        raise VoxlateError('角色数据格式无效或数量超限。')
    names = {r['name'].casefold() for r in project['roles']}
    for role in project['roles']:
        compact = re.sub(r'^角色 ([A-Z]{1,2}|[1-9][0-9]?)$', r'角色\1', role['name'])
        if compact != role['name'] and compact.casefold() not in names:
            names.add(compact.casefold())
            role['name'] = compact
    default = project['roles'][0]['id']
    for segment in project.get('segments', []):
        segment.setdefault('role_id', default)


def reference_segment(project, role_id):
    role = next((r for r in project.get('roles', []) if r['id'] == role_id), None)
    if role is None:
        raise VoxlateError('句子的角色不存在，请重新分配。')
    segments = project.get('segments', [])
    explicit = role.get('reference_sentence_id')
    if explicit is not None:
        selected = next((s for s in segments if s['id'] == explicit), None)
        if selected is None:
            raise VoxlateError('角色参考句不存在。')
        return selected
    members = [s for s in segments if s.get('role_id') == role_id]
    if not members:
        raise VoxlateError('请先为角色分配句子或选择参考句。')
    return max(members, key=lambda s: s['end']-s['start'])
