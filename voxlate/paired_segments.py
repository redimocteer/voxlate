"""Ordered cut pairs with a bounded source interval and reversible edits."""
import copy

from .common import VoxlateError
from .segmentation import MAX_SPAN, MIN_BLOCK, validate_blocks

COLORS = ('#3466d6', '#008675', '#a45aba', '#bf7026', '#478e36',
          '#c44878', '#547ba0', '#887629', '#775bd3', '#ad5044')
MAX_CUTS = 20


class PairedSegmentPlan:
    def __init__(self, project, first, last, blocks=None):
        self.project = project
        rows = project['segments']
        self.first, self.last = first, last
        self.start, self.end = self.context(first, last)
        self.cuts = [float(t) for row in rows[first:last+1] for t in (row['start'], row['end'])]
        if blocks is not None:
            validate_blocks(blocks, project['duration'])
            self.start, self.end = blocks[0]['start'], blocks[-1]['end']
            self.cuts = [float(t) for b in blocks if b['enabled'] or not b.get('omit_row', True) for t in (b['start'], b['end'])]
            touched = [i for i, row in enumerate(rows) if row['end'] > self.start and row['start'] < self.end]
            if touched:
                self.first, self.last = touched[0], touched[-1]
        self.check()
        self.undo_stack, self.redo_stack = [], []

    def context(self, first, last):
        rows = self.project['segments']
        left = rows[first-1]['end'] if first else 0
        right = rows[last+1]['start'] if last+1 < len(rows) else self.project['duration']
        return (left+rows[first]['start'])/2, (rows[last]['end']+right)/2

    def check(self):
        if len(self.cuts) > MAX_CUTS:
            raise VoxlateError('一次最多编辑 10 句（20 条切线），请缩小选区。')
        if self.end-self.start > MAX_SPAN:
            raise VoxlateError('一次最多编辑 10 分钟，请缩小选区。')

    @property
    def pairs(self):
        return list(zip(self.cuts[::2], self.cuts[1::2]))

    def number(self, index):
        return self.project['segments'][self.first]['id']+index

    @property
    def blocks(self):
        """Explicit gaps are commit metadata, never numbered project rows."""
        blocks, cursor = [], self.start
        for start, end in self.pairs:
            if start > cursor:
                blocks.append(dict(start=cursor, end=start, enabled=False, text='', omit_row=True))
            if end > start:
                original = next((row for row in self.project['segments']
                                 if abs(row['start']-start) < 1e-6 and abs(row['end']-end) < 1e-6), None)
                blocks.append(dict(start=start, end=end, enabled=original.get('enabled', True) if original else True,
                                   text='', manual_text=False, omit_row=False))
            cursor = end
        if cursor < self.end:
            blocks.append(dict(start=cursor, end=self.end, enabled=False, text='', omit_row=True))
        return blocks

    def snapshot(self):
        return (self.first, self.last, self.start, self.end, self.cuts[:])

    def restore(self, state):
        self.first, self.last, self.start, self.end, self.cuts = copy.deepcopy(state)

    def remember(self, previous):
        if previous != self.snapshot():
            self.undo_stack = (self.undo_stack+[previous])[-100:]
            self.redo_stack.clear()

    def undo(self):
        if self.undo_stack:
            self.redo_stack.append(self.snapshot())
            self.restore(self.undo_stack.pop())

    def redo(self):
        if self.redo_stack:
            self.undo_stack.append(self.snapshot())
            self.restore(self.redo_stack.pop())

    def split(self, seconds):
        if len(self.cuts) >= MAX_CUTS:
            raise VoxlateError('最多 20 条切线，请先删除不需要的切线。')
        seconds = max(self.start, min(self.end, round(seconds, 3)))
        if any(abs(t-seconds) < MIN_BLOCK-1e-6 for t in self.cuts):
            raise VoxlateError('新切线须离已有切线至少 0.1 秒。')
        previous = self.snapshot()
        self.cuts.append(seconds)
        self.cuts.sort()
        self.remember(previous)
        return self.cuts.index(seconds)

    def merge(self, cut):
        if 0 <= cut < len(self.cuts):
            previous = self.snapshot()
            self.cuts.pop(cut)
            self.remember(previous)

    def move_cut(self, cut, seconds):
        # Two adjacent sentences may share an endpoint, but a pair cannot collapse.
        lower = self.start if cut == 0 else self.cuts[cut-1]+(MIN_BLOCK if cut % 2 else 0)
        upper = self.end if cut+1 == len(self.cuts) else self.cuts[cut+1]-(MIN_BLOCK if cut % 2 == 0 else 0)
        if lower <= upper:
            self.cuts[cut] = max(lower, min(upper, round(seconds, 3)))

    def add_sentence(self, start, end):
        start, end = sorted((max(self.start, min(self.end, round(start, 3))),
                             max(self.start, min(self.end, round(end, 3)))))
        if end-start < MIN_BLOCK-1e-6:
            raise VoxlateError('拖选范围至少 0.1 秒。')
        if len(self.cuts) >= MAX_CUTS:
            raise VoxlateError('最多 10 句，请先删除不需要的句子。')
        if any(start < hi-1e-6 and end > lo+1e-6 for lo, hi in self.pairs):
            raise VoxlateError('范围与已有句子重叠，请先删除或调整该句。')
        previous = self.snapshot()
        pairs = sorted(self.pairs+[(start, end)])
        self.cuts = [point for pair in pairs for point in pair]
        self.remember(previous)
        return pairs.index((start, end))

    def delete_sentence(self, index):
        if 0 <= index < len(self.pairs):
            previous = self.snapshot()
            del self.cuts[index*2:index*2+2]
            self.remember(previous)

    def extend_sentence(self, side):
        if len(self.cuts) % 2:
            raise VoxlateError('请先补齐或删除最右侧的落单切线。')
        index = self.first-1 if side == 'start' else self.last+1
        rows = self.project['segments']
        if not 0 <= index < len(rows):
            return False
        previous = self.snapshot()
        if side == 'start':
            self.first = index
        else:
            self.last = index
        self.start, self.end = self.context(self.first, self.last)
        self.cuts = sorted(self.cuts+[rows[index]['start'], rows[index]['end']])
        try:
            self.check()
        except Exception:
            self.restore(previous)
            raise
        self.remember(previous)
        return True

    def validate(self):
        self.check()
        if len(self.cuts) % 2:
            raise VoxlateError('最右侧切线尚未配对，请补一条或删除它。')
        if any(end <= start for start, end in self.pairs):
            raise VoxlateError('每句话的结束切线须在开始切线之后。')
        validate_blocks(self.blocks, self.project['duration'])
