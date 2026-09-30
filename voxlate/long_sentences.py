"""Conservative, model-free refinement of newly recognized long sentences."""
import copy
import math
import re

LONG_SENTENCE_SECONDS = 8.0


def split_long_sentence(row):
    """Keep the complete row unless word times and a natural boundary agree."""
    if (row['end'] - row['start'] <= LONG_SENTENCE_SECONDS
            or not row.get('enabled', True)
            or any(row.get(key) for key in ('timing_uncertain', 'timing_fallback',
                                            'auto_preserve_original', 'manual_boundary'))
            or row.get('target_text', '').strip()):
        return [row]
    words = row.get('words', [])
    if not words or ''.join(w.get('word', '') for w in words).strip() != row['source_text'].strip():
        return [row]
    previous = row['start']
    for word in words:
        start, end = word.get('start'), word.get('end')
        if (not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (start, end))
                or start < previous - 1e-6 or end < start or end > row['end'] + 1e-6
                or end - start > 4):
            return [row]
        previous = end
    zero = sum(w['end'] - w['start'] <= .001 for w in words)
    if zero == len(words) or (zero >= 3 and zero * 2 >= len(words)):
        return [row]
    groups, first = [], 0
    for index in range(1, len(words)):
        left, right = words[index - 1], words[index]
        elapsed = left['end'] - words[first]['start']
        gap = right['start'] - left['end']
        fragment = left['word'].rstrip().rstrip('"\'”’」』）)')
        clause = bool(re.search(r'[,;:、，；：]$', fragment))
        ending = bool(re.search(r'[。！？!?]$|(?<!\.)\.$', fragment))
        abbreviation = re.fullmatch(r'\s*(?:(?:Mr|Mrs|Ms|Dr|Prof|St|Jr|Sr)\.|(?:[A-Za-z]\.)+)', fragment, re.I)
        boundary = (clause and (gap >= .4 - 1e-6 or (elapsed >= 3 and gap >= .12 - 1e-6)))
        boundary |= ending and not abbreviation and gap >= .12 - 1e-6
        if (boundary and elapsed >= 1.2 - 1e-6 and words[-1]['end'] - right['start'] >= 1
                and left['end'] > left['start'] and right['end'] > right['start']):
            groups.append(words[first:index])
            first = index
    groups.append(words[first:])
    if len(groups) == 1:
        return [row]
    result = []
    for group in groups:
        part = copy.deepcopy(row)
        part.update(start=group[0]['start'], end=group[-1]['end'], words=copy.deepcopy(group),
                    source_text=''.join(w['word'] for w in group).strip(), auto_split_long=True)
        if all('probability' in w for w in group):
            part['confidence'] = sum(w['probability'] for w in group) / len(group)
        result.append(part)
    return result


def refine_long_sentences(rows):
    """Run only on fresh ASR results, before translation, roles or dubbing exist."""
    result, changed = [], 0
    for row in rows:
        parts = split_long_sentence(row)
        changed += len(parts) > 1
        result.extend(copy.deepcopy(parts))
    for number, row in enumerate(result, 1):
        row['id'] = number
    return result, changed
