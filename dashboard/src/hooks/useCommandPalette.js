import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { useGateway } from './useGateway';

/** Static command definitions */
const COMMANDS = [
  { id: 'cmd:chat',      label: 'Open chat',       group: 'Commands', icon: '💬' },
  { id: 'cmd:status',    label: 'System status',    group: 'Commands', icon: '🔴' },
  { id: 'cmd:budget',    label: 'Budget overview',  group: 'Commands', icon: '💰' },
  { id: 'cmd:approvals', label: 'Approvals queue',  group: 'Commands', icon: '✅' },
  { id: 'cmd:voice',     label: 'Toggle voice',     group: 'Commands', icon: '🎤' },
  { id: 'cmd:memory',    label: 'Memory search',    group: 'Commands', icon: '🧠' },
  { id: 'cmd:skills',    label: 'Skills panel',     group: 'Commands', icon: '⚡' },
  { id: 'cmd:kanban',    label: 'Kanban board',     group: 'Commands', icon: '📋' },
  { id: 'cmd:settings',  label: 'Settings',         group: 'Commands', icon: '⚙️' },
];

/** Static agent definitions */
const AGENTS = [
  { id: 'agent:researcher',       label: 'Researcher',        group: 'Agents', icon: '🔍' },
  { id: 'agent:coder',            label: 'Coder',             group: 'Agents', icon: '⌨️' },
  { id: 'agent:reviewer',         label: 'Reviewer',          group: 'Agents', icon: '👀' },
  { id: 'agent:memory-keeper',   label: 'Memory-keeper',    group: 'Agents', icon: '🧠' },
  { id: 'agent:security-reviewer', label: 'Security reviewer', group: 'Agents', icon: '🔒' },
];

const RESULT_LIMIT = 5;

/**
 * Simple fuzzy score: higher = better match.
 * Returns 0 if no match.
 */
function fuzzyScore(query, text) {
  if (!query) return 1;
  const q = query.toLowerCase();
  const t = text.toLowerCase();
  if (t.includes(q)) return 3;
  // token overlap
  const tokens = q.split(/\s+/).filter(Boolean);
  const matched = tokens.filter((tok) => t.includes(tok)).length;
  if (matched > 0) return matched / tokens.length;
  return 0;
}

/**
 * useCommandPalette — orchestrates command palette state.
 *
 * Returns { isOpen, open, close, query, setQuery, results, selectedIndex,
 *           selectNext, selectPrev, execute, activeGroup }
 */
export function useCommandPalette(token) {
  const { get } = useGateway(token);

  const [isOpen, setIsOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [debouncedQuery, setDebouncedQuery] = useState('');
  const [skills, setSkills] = useState([]);
  const [memoryItems, setMemoryItems] = useState([]);
  const [pendingApprovals, setPendingApprovals] = useState([]);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [activeGroup, setActiveGroup] = useState(null);

  // Debounce query 150ms
  const debounceTimer = useRef(null);
  const handleSetQuery = useCallback((q) => {
    setQuery(q);
    clearTimeout(debounceTimer.current);
    debounceTimer.current = setTimeout(() => setDebouncedQuery(q), 150);
  }, []);

  // Load skills, memory stats, approvals when opened
  useEffect(() => {
    if (!isOpen) return;
    let mounted = true;

    get('/skills?pinned=true')
      .then((d) => { if (mounted) setSkills(d.pinned || []); })
      .catch(() => { if (mounted) setSkills([]); });

    get('/memory/stats')
      .then((d) => { if (mounted) setMemoryItems(d.recent || []); })
      .catch(() => { if (mounted) setMemoryItems([]); });

    get('/approvals')
      .then((d) => { if (mounted) setPendingApprovals(Array.isArray(d) ? d : d.pending || []); })
      .catch(() => { if (mounted) setPendingApprovals([]); });

    return () => { mounted = false; };
  }, [isOpen, get]);

  // Reset on close
  useEffect(() => {
    if (!isOpen) {
      setQuery('');
      setDebouncedQuery('');
      setSelectedIndex(0);
      setActiveGroup(null);
    }
  }, [isOpen]);

  // Build flat result list + group map
  const { flatResults, groupMap } = useMemo(() => {
    const q = debouncedQuery;

    const skillResults = skills
      .map((s) => ({ id: `skill:${s}`, label: s, group: 'Skills', icon: '⚡' }))
      .filter((item) => fuzzyScore(q, item.label) > 0)
      .sort((a, b) => fuzzyScore(q, b.label) - fuzzyScore(q, a.label))
      .slice(0, RESULT_LIMIT);

    const memoryResults = memoryItems
      .map((m) => ({
        id: `memory:${m.id || m}`,
        label: m.text || String(m),
        group: 'Memory',
        icon: '🧠',
        description: m.type || '',
      }))
      .filter((item) => fuzzyScore(q, item.label) > 0)
      .slice(0, RESULT_LIMIT);

    const commandResults = COMMANDS
      .filter((item) => fuzzyScore(q, item.label) > 0)
      .slice(0, RESULT_LIMIT);

    const approvalResults = pendingApprovals
      .map((a) => ({
        id: `approval:${a.id}`,
        label: a.summary || `Approval #${a.id}`,
        group: 'Approvals',
        icon: '✅',
        description: a.agent || '',
      }))
      .filter((item) => fuzzyScore(q, item.label) > 0)
      .slice(0, RESULT_LIMIT);

    const agentResults = AGENTS
      .filter((item) => fuzzyScore(q, item.label) > 0)
      .slice(0, RESULT_LIMIT);

    const groups = [
      { key: 'Skills',    items: skillResults },
      { key: 'Memory',    items: memoryResults },
      { key: 'Commands',  items: commandResults },
      { key: 'Approvals', items: approvalResults },
      { key: 'Agents',    items: agentResults },
    ].filter((g) => g.items.length > 0);

    const groupMap = {};
    groups.forEach((g) => {
      groupMap[g.key] = g.items;
    });

    const flatResults = groups.flatMap((g) => g.items);
    return { flatResults, groupMap };
  }, [debouncedQuery, skills, memoryItems, pendingApprovals]);

  const open = useCallback(() => setIsOpen(true), []);
  const close = useCallback(() => setIsOpen(false), []);

  const selectNext = useCallback(() => {
    setSelectedIndex((i) => Math.min(i + 1, flatResults.length - 1));
  }, [flatResults.length]);

  const selectPrev = useCallback(() => {
    setSelectedIndex((i) => Math.max(i - 1, 0));
  }, []);

  const execute = useCallback((item) => {
    close();
    // Dispatch is handled by the caller via a callback prop;
    // here we just close — caller receives the item via onExecute.
    return item;
  }, [close]);

  return {
    isOpen,
    open,
    close,
    query,
    setQuery: handleSetQuery,
    results: flatResults,
    groupMap,
    selectedIndex,
    setSelectedIndex,
    selectNext,
    selectPrev,
    execute,
    activeGroup,
    setActiveGroup,
  };
}
