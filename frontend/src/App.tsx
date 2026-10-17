// App shell — routing + cross-cutting concerns only (thin by spec §6).
import { AnimatePresence, motion } from 'framer-motion'
import { lazy, Suspense, useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Toaster } from 'sonner'
import { SkeletonCard } from '@/components/ui'
import { CommandPalette } from '@/components/layout/CommandPalette'
import { Sidebar, TABS, type TabKey } from '@/components/layout/Sidebar'
import { useDiscussionStream } from '@/hooks'
import Setup from '@/pages/Setup'

// Pages are code-split: each tab's code loads on first visit, not up front.
const Analysis = lazy(() => import('@/pages/Analysis'))
const Discover = lazy(() => import('@/pages/Discover'))
const Discussions = lazy(() => import('@/pages/Discussions'))
const Guide = lazy(() => import('@/pages/Guide'))
const Portfolio = lazy(() => import('@/pages/Portfolio'))
const Settings = lazy(() => import('@/pages/Settings'))
const Watchlist = lazy(() => import('@/pages/Watchlist'))

// URL ↔ state (survives refresh; ?symbol=PLTR&tab=analysis is shareable)
const TAB_KEYS = new Set<string>(TABS.map((t) => t.key))

function stateFromUrl(): { tab: TabKey; symbol: string | null } {
  const q = new URLSearchParams(window.location.search)
  const rawTab = q.get('tab') ?? 'analysis'
  const rawSym = (q.get('symbol') ?? '').toUpperCase()
  return {
    tab: (TAB_KEYS.has(rawTab) ? rawTab : 'analysis') as TabKey,
    symbol: /^[A-Z.\-]{1,10}$/.test(rawSym) ? rawSym : null,
  }
}

function urlFor(tab: TabKey, symbol: string | null): string {
  const q = new URLSearchParams()
  if (tab !== 'analysis') q.set('tab', tab)
  // symbol belongs to the Analysis view only — other tabs don't use it, so
  // their URLs shouldn't claim it (it stays in memory for the tab-back trip)
  if (symbol && tab === 'analysis') q.set('symbol', symbol)
  const qs = q.toString()
  return qs ? `?${qs}` : window.location.pathname
}

export default function App() {
  const initial = stateFromUrl()
  const [tab, setTabState] = useState<TabKey>(initial.tab)
  // First-run gate: no config → the setup wizard IS the app
  const { data: setupStatus, refetch: refetchSetup } = useQuery({
    queryKey: ['setup-status'],
    queryFn: async () => (await fetch('/api/setup/status')).json(),
  })
  // Capability gating: tabs whose backing capability is off get hidden
  const { data: caps } = useQuery({
    queryKey: ['capabilities'],
    queryFn: async () => (await fetch('/api/capabilities')).json(),
    enabled: setupStatus?.configured === true,
  })
  const [symbol, setSymbol] = useState<string | null>(initial.symbol)

  // tab switches replace (Back shouldn't walk every tab flip)
  const setTab = (t: TabKey) => {
    setTabState(t)
    window.history.replaceState(null, '', urlFor(t, symbol))
  }

  // Back/Forward restore both tab and ticker
  useEffect(() => {
    const onPop = () => {
      const st = stateFromUrl()
      setTabState(st.tab)
      // only analysis URLs carry a symbol; keep the in-memory one otherwise
      if (st.tab === 'analysis') setSymbol(st.symbol)
    }
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])

  // SSE: discussions written by the user's agent appear in the UI within a second.
  useDiscussionStream()

  if (setupStatus && !setupStatus.configured) {
    return (<><Setup onDone={() => refetchSetup()} />
      <Toaster theme="dark" position="bottom-right" /></>)
  }
  const visibleTabs = TABS.filter((t) =>
    t.key !== 'portfolio' || caps?.account?.available !== false)

  const analyze = (s: string) => {
    const sym = s.toUpperCase()
    setSymbol(sym)
    setTabState('analysis')
    // push (not replace): Back walks your ticker research history
    window.history.pushState(null, '', urlFor('analysis', sym))
  }

  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar tab={tab} setTab={setTab} tabs={visibleTabs} />
      <main className="flex flex-1 flex-col overflow-hidden">
        {setupStatus?.user_name && (
          <div className="flex shrink-0 justify-end border-b border-border px-6 py-1.5">
            <span className="rounded-full bg-surface-2 px-2.5 py-0.5 text-[11px] text-muted">
              {setupStatus.user_name}&rsquo;s Portfolio
            </span>
          </div>
        )}
        <div className="flex-1 overflow-y-auto px-6 py-5">
          {/* smooth page transitions between tabs (polish requirement) */}
          <AnimatePresence mode="wait">
            <motion.div
              key={tab}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -8 }}
              transition={{ duration: 0.18 }}
              className="h-full">
              <Suspense fallback={<div className="space-y-4"><SkeletonCard lines={2} /><SkeletonCard lines={6} /></div>}>
                {tab === 'analysis' && <Analysis symbol={symbol} setSymbol={analyze} />}
                {tab === 'portfolio' && <Portfolio onAnalyze={analyze} />}
                {tab === 'discover' && <Discover onAnalyze={analyze} />}
                {tab === 'watchlist' && <Watchlist onAnalyze={analyze} />}
                {tab === 'discussions' && <Discussions />}
                {tab === 'settings' && <Settings />}
                {tab === 'guide' && <Guide />}
              </Suspense>
            </motion.div>
          </AnimatePresence>
        </div>
        <footer className="border-t border-border px-6 py-1.5 text-[10px] text-faint">
          {TABS.find((t) => t.key === tab)?.label} · ⌘K to search ·
          research &amp; education only — no direction predictions, by design
        </footer>
      </main>
      <CommandPalette onTicker={analyze} onTab={setTab} />
      <Toaster theme="dark" position="bottom-right"
        toastOptions={{ style: { background: 'var(--color-surface-2)', border: '1px solid var(--color-border)', color: 'var(--color-text)' } }} />
    </div>
  )
}
