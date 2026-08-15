'use strict';

/**
 * Live, client-side search for the unified App Config page.
 *
 * Search is intentionally broader than literal substring matching:
 * common word forms share a stem, related words share a small thesaurus, and
 * longer words tolerate a small typo. The DOM filter targets shared config
 * primitives, so rows injected after load are searchable too.
 */

const _STOP_WORDS = new Set([
  'a', 'an', 'and', 'are', 'at', 'for', 'from', 'how', 'in', 'is', 'it',
  'my', 'of', 'on', 'or', 'the', 'to', 'with',
]);

const _RELATED_FAMILIES = [
  ['agent', 'assistant', 'bot', 'webagent'],
  ['app', 'application', 'workspace'],
  ['attachment', 'file', 'document', 'upload'],
  ['auth', 'authentication', 'login', 'signin', 'oauth'],
  ['automation', 'job', 'schedule', 'scheduler', 'task', 'timer'],
  ['boot', 'launch', 'startup', 'start'],
  ['cat', 'cats', 'feline', 'kitten', 'kittens'],
  ['chat', 'conversation', 'message', 'messaging'],
  ['connector', 'integration', 'provider', 'service'],
  ['credential', 'key', 'password', 'secret', 'token'],
  ['database', 'data', 'storage'],
  ['delete', 'erase', 'purge', 'remove'],
  ['design', 'appearance', 'color', 'colour', 'style', 'theme'],
  ['extension', 'addon', 'plugin'],
  ['guard', 'protection', 'safety', 'security'],
  ['llm', 'model'],
  ['market', 'marketplace', 'store'],
  ['memory', 'context', 'recall'],
  ['network', 'remote', 'domain', 'dns'],
  ['person', 'account', 'member', 'user'],
  ['social', 'community', 'media'],
  ['speech', 'dictation', 'microphone', 'voice'],
];

const _RELATED = new Map();
for (const family of _RELATED_FAMILIES) {
  const words = new Set(family);
  for (const word of family) _RELATED.set(word, words);
}

const _PHRASE_ALIASES = [
  [/\bsign[\s-]+in\b/g, 'signin'],
  [/\blog[\s-]+in\b/g, 'login'],
  [/\bdata[\s-]+base\b/g, 'database'],
  [/\bstart[\s-]+up\b/g, 'startup'],
  [/\bweb[\s-]+agent\b/g, 'webagent'],
  [/\bsocial[\s-]+media\b/g, 'social'],
];

function _normalize(value) {
  let text = String(value || '')
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase();
  for (const [pattern, replacement] of _PHRASE_ALIASES) {
    text = text.replace(pattern, replacement);
  }
  return text.replace(/[^a-z0-9]+/g, ' ').trim();
}

function _stem(word) {
  if (word.length > 5 && word.endsWith('ies')) return word.slice(0, -3) + 'y';
  if (word.length > 5 && word.endsWith('ing')) {
    let base = word.slice(0, -3);
    if (base.length > 3 && base.at(-1) === base.at(-2)) base = base.slice(0, -1);
    return base;
  }
  if (word.length > 4 && word.endsWith('ed')) {
    let base = word.slice(0, -2);
    if (base.length > 3 && base.at(-1) === base.at(-2)) base = base.slice(0, -1);
    return base;
  }
  if (word.length > 3 && word.endsWith('s') && !word.endsWith('ss')) {
    return word.slice(0, -1);
  }
  return word;
}

function _tokens(value, { query = false } = {}) {
  const words = _normalize(value).split(' ').filter(Boolean);
  return query ? words.filter(word => !_STOP_WORDS.has(word)) : words;
}

function _distanceWithin(left, right, limit) {
  if (Math.abs(left.length - right.length) > limit) return false;
  let previous = Array.from({ length: right.length + 1 }, (_, i) => i);
  for (let i = 1; i <= left.length; i++) {
    const current = [i];
    let rowMin = current[0];
    for (let j = 1; j <= right.length; j++) {
      current[j] = Math.min(
        current[j - 1] + 1,
        previous[j] + 1,
        previous[j - 1] + (left[i - 1] === right[j - 1] ? 0 : 1),
      );
      rowMin = Math.min(rowMin, current[j]);
    }
    if (rowMin > limit) return false;
    previous = current;
  }
  return previous[right.length] <= limit;
}

function _wordMatches(queryWord, candidateWord) {
  const queryStem = _stem(queryWord);
  const candidateStem = _stem(candidateWord);
  if (queryWord === candidateWord || queryStem === candidateStem) return true;

  const related = _RELATED.get(queryWord) || _RELATED.get(queryStem);
  if (related && (related.has(candidateWord) || related.has(candidateStem))) return true;

  // Avoid prefix/typo matching for tiny words so "cat" never matches "chat".
  if (Math.min(queryStem.length, candidateStem.length) >= 4
      && (queryStem.startsWith(candidateStem) || candidateStem.startsWith(queryStem))) {
    return true;
  }

  const typoLimit = queryStem.length >= 8 ? 2 : (queryStem.length >= 4 ? 1 : 0);
  return typoLimit > 0 && _distanceWithin(queryStem, candidateStem, typoLimit);
}

/** Exported for focused tests and reuse by other config surfaces. */
export function matchesRelatedText(query, candidate) {
  const queryText = _normalize(query);
  if (!queryText) return true;
  const queryWords = _tokens(queryText, { query: true });
  if (!queryWords.length) return true;

  const candidateText = _normalize(candidate);
  if (queryWords.length > 1 && candidateText.includes(queryWords.join(' '))) return true;
  const candidateWords = _tokens(candidateText);
  return queryWords.every(queryWord =>
    candidateWords.some(candidateWord => _wordMatches(queryWord, candidateWord)));
}

function _searchText(element) {
  const extras = [];
  for (const node of element.querySelectorAll(
    '[data-search-terms], [aria-label], [title], input[placeholder], textarea[placeholder], select',
  )) {
    extras.push(
      node.dataset.searchTerms || '',
      node.getAttribute('aria-label') || '',
      node.getAttribute('title') || '',
      node.getAttribute('placeholder') || '',
    );
  }
  return [
    element.textContent || '',
    element.id || '',
    element.dataset.searchTerms || '',
    element.dataset.paArea || '',
    ...extras,
  ].join(' ');
}

function _setHidden(element, hidden) {
  if (!element) return;
  if (hidden) element.setAttribute('data-ac-search-hidden', 'true');
  else element.removeAttribute('data-ac-search-hidden');
}

function _directHead(row) {
  return [...row.children].find(child =>
    child.matches?.('.ac-ability-row, .ac-group-head')) || null;
}

/**
 * @param {{root:HTMLElement, empty:HTMLElement}} opts
 * @returns {{filter:(query:string)=>number, clear:()=>void, destroy:()=>void}}
 */
export function createAppConfigSearch({ root, empty = null } = {}) {
  let currentQuery = '';
  let observerFrame = null;

  function clear() {
    currentQuery = '';
    root?.classList.remove('ac-searching');
    root?.querySelectorAll('[data-ac-search-hidden], [data-ac-search-descendant]')
      .forEach(element => {
        element.removeAttribute('data-ac-search-hidden');
        element.removeAttribute('data-ac-search-descendant');
      });
    if (empty) {
      empty.hidden = true;
      empty.textContent = '';
    }
  }

  function filter(rawQuery) {
    if (!root) return 0;
    const query = String(rawQuery || '').trim();
    // Keep the page completely unchanged until the user has entered four
    // meaningful characters. Short fragments are too noisy to filter well.
    if (_normalize(query).replace(/\s/g, '').length < 4) {
      clear();
      return 0;
    }
    currentQuery = query;

    root.classList.add('ac-searching');
    const resultElements = [...root.querySelectorAll('.ac-ability-row, .ac-group-head')];
    const matched = new Set(
      resultElements.filter(element => matchesRelatedText(query, _searchText(element))),
    );

    // A category-name match represents the whole category; a section-name match
    // represents the whole section. Otherwise only matching rows survive.
    const sectionNames = {
      'ac-section-data-settings': 'Data Settings',
      'ac-section-app-settings': 'App Settings application',
      'ac-section-agent-settings': 'Agent Settings assistant bot',
    };
    for (const section of root.querySelectorAll('.ac-section')) {
      const sectionMatch = matchesRelatedText(query, sectionNames[section.id] || '');
      for (const category of section.querySelectorAll('.ac-category-group')) {
        const summary = [...category.children]
          .find(child => child.classList?.contains('ac-category-summary'));
        const categoryMatch = sectionMatch
          || (summary && matchesRelatedText(query, _searchText(summary)));
        if (categoryMatch) {
          category.querySelectorAll('.ac-ability-row, .ac-group-head')
            .forEach(element => matched.add(element));
        }
      }
    }

    for (const element of resultElements) _setHidden(element, !matched.has(element));

    // Process nested wrappers inside-out. A matching descendant keeps its parent
    // header as context and forces only that nesting path open.
    const rows = [...root.querySelectorAll('.ac-row')].reverse();
    for (const row of rows) {
      row.removeAttribute('data-ac-search-descendant');
      const head = _directHead(row);
      const ownHit = !!head && matched.has(head);
      const descendantHit = [...row.querySelectorAll('.ac-ability-row, .ac-group-head')]
        .some(element => element !== head && matched.has(element));
      _setHidden(row, !ownHit && !descendantHit);
      if (descendantHit) {
        row.setAttribute('data-ac-search-descendant', 'true');
        _setHidden(head, false);
      }
    }

    let visibleCount = 0;
    for (const section of root.querySelectorAll('.ac-section')) {
      let sectionHasResults = false;
      for (const category of section.querySelectorAll('.ac-category-group')) {
        const categoryMatches = [...category.querySelectorAll('.ac-ability-row, .ac-group-head')]
          .filter(element => matched.has(element));
        _setHidden(category, categoryMatches.length === 0);
        if (categoryMatches.length) {
          sectionHasResults = true;
          visibleCount += categoryMatches.length;
        }
      }
      _setHidden(section, !sectionHasResults);
    }

    if (empty) {
      empty.hidden = visibleCount > 0;
      const message = `No match for "${query}"`;
      if (empty.textContent !== message) empty.textContent = message;
    }
    return visibleCount;
  }

  const observer = root && typeof MutationObserver !== 'undefined'
    ? new MutationObserver(() => {
        if (!currentQuery || observerFrame) return;
        observerFrame = requestAnimationFrame(() => {
          observerFrame = null;
          filter(currentQuery);
        });
      })
    : null;
  observer?.observe(root, { childList: true, subtree: true });

  return {
    filter,
    clear,
    destroy() {
      observer?.disconnect();
      if (observerFrame) cancelAnimationFrame(observerFrame);
      clear();
    },
  };
}
