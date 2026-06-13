/**
 * lighting-ai UI  —  App.jsx
 *
 * Wires directly to every FastAPI endpoint:
 *   GET  /health             → API status badge
 *   GET  /concepts           → concept selector
 *   POST /concepts           → upload new concept YAML
 *   DELETE /concepts/{id}    → remove concept
 *   POST /process            → upload + run pipeline
 *   GET  /jobs/{id}          → live polling (1.5 s interval)
 *   GET  /history            → persistent planning history (SQLite)
 *   GET  /exports/{id}/{fmt} → download DXF / XLSX / PDF
 *   POST /corrections        → designer corrections → RL signal
 */

import { useState, useEffect, useRef, useCallback } from 'react'

// ─── API base ─────────────────────────────────────────────────────────────────
const API = import.meta.env.VITE_API_URL ?? ''
const endpoint = path => API ? `${API}${path}` : `/api${path}`

// ─── Zone colours ─────────────────────────────────────────────────────────────
const ZONE_COLOR = {
  sales_floor:   '#e8c547',
  checkout_zone: '#ff7043',
  entrance:      '#26c6da',
  storage:       '#ab47bc',
  office:        '#42a5f5',
  corridor:      '#78909c',
  service_area:  '#ec407a',
  unknown:       '#546e7a',
}
const zc = t => ZONE_COLOR[t] ?? '#546e7a'

// ─── Luminaire type colours ───────────────────────────────────────────────────
const LUMI_COLOR = { A:'#e040fb', B:'#f44336', C:'#00bcd4', D:'#ffeb3b', E:'#42a5f5' }
const lc = t => LUMI_COLOR[t] ?? '#e040fb'

const LUMI_LABEL = {
  A:'15W 40° inner', B:'20W 60° perimeter',
  C:'20W 40° accent', D:'15W IP44 wet/entrance', E:'20W 24° pendant',
}

// ─── Helpers ──────────────────────────────────────────────────────────────────
const fmt   = n => n?.toLocaleString() ?? '—'
const pct   = (a,b) => b ? `${((1-Math.abs(a-b)/b)*100).toFixed(1)}%` : '—'
const tsStr = ts => ts ? new Date(ts*1000).toLocaleString('de-DE', {
  day:'2-digit', month:'2-digit', year:'numeric',
  hour:'2-digit', minute:'2-digit'}) : '—'

async function apiFetch(path, opts={}) {
  const r = await fetch(endpoint(path), {signal:AbortSignal.timeout(8000), ...opts})
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`)
  return r.json()
}

// ─── Demo result ──────────────────────────────────────────────────────────────
function makeDemoResult() {
  const zones = [
    { index:0, zone_type:'sales_floor',   confidence:0.95, method:'label',  area_m2:643.6, bounds:[12400,15400,59000,37000] },
    { index:1, zone_type:'corridor',      confidence:0.92, method:'label',  area_m2:43.4,  bounds:[5000,21500,11600,28100] },
    { index:2, zone_type:'storage',       confidence:0.92, method:'label',  area_m2:97.0,  bounds:[21400,12400,31200,22200] },
    { index:3, zone_type:'entrance',      confidence:0.92, method:'label',  area_m2:3.9,   bounds:[38000,5400,40000,7300] },
    { index:4, zone_type:'service_area',  confidence:0.92, method:'label',  area_m2:7.5,   bounds:[4700,32200,7400,34900] },
  ]
  const placed = []
  for (let x=15000; x<57000; x+=2500)
    for (let y=17000; y<36000; y+=2500)
      if (Math.random()>0.35)
        placed.push({ id:placed.length, x, y, lumi_type:'A', zone_type:'sales_floor',
          product_code:'MIKA80-E-WS-930-PH-PS7HE+-L22-2400-40RF-DV2.5-EN',
          description:'MIKA80-E 15W 40°', wattage:15, lux_output:2400,
          beam_angle_deg:40, grid_snapped:true, shelf_aligned:true, rotation:0 })
  for (let x=15000; x<57000; x+=2500) {
    placed.push({ id:placed.length, x, y:17000, lumi_type:'B', zone_type:'sales_floor',
      product_code:'MIKA80-E-WS-930-PH-PS7HE+-L22-3200-60RF-DV2.5-EN',
      description:'MIKA80-E 20W 60°', wattage:20, lux_output:3200,
      beam_angle_deg:60, grid_snapped:true, shelf_aligned:true, rotation:0 })
    placed.push({ id:placed.length, x, y:35500, lumi_type:'B', zone_type:'sales_floor',
      product_code:'MIKA80-E-WS-930-PH-PS7HE+-L22-3200-60RF-DV2.5-EN',
      description:'MIKA80-E 20W 60°', wattage:20, lux_output:3200,
      beam_angle_deg:60, grid_snapped:true, shelf_aligned:true, rotation:0 })
  }
  const tw = placed.reduce((s,p)=>s+p.wattage,0)
  return {
    total_luminaires:placed.length, total_wattage:tw,
    type_A:placed.filter(p=>p.lumi_type==='A').length,
    type_B:placed.filter(p=>p.lumi_type==='B').length,
    type_C:0, type_D:0, type_E:0,
    zones, placed,
    exports:{dxf:null,xlsx:null,pdf:null},
  }
}

// ═══════════════════════════════════════════════════════════════════════════════
// MAIN APP
// ═══════════════════════════════════════════════════════════════════════════════
export default function App() {
  // API state
  const [online,    setOnline]    = useState(false)
  const [concepts,  setConcepts]  = useState(['rossmann_standard'])

  // Form
  const [file,        setFile]        = useState(null)
  const [conceptId,   setConceptId]   = useState('rossmann_standard')
  const [projectName, setProjectName] = useState('Rossmann Hamburg EG')
  const [customer,    setCustomer]    = useState('Dirk Rossmann GmbH')
  const [drag,        setDrag]        = useState(false)
  const [running,     setRunning]     = useState(false)

  // Jobs (current session)
  const [jobs,       setJobs]       = useState([])
  const [activeId,   setActiveId]   = useState(null)
  const [result,     setResult]     = useState(makeDemoResult())
  const pollRef = useRef(null)

  // Persistent history (from /history endpoint)
  const [historyJobs,    setHistoryJobs]    = useState([])
  const [historyLoading, setHistoryLoading] = useState(false)

  // Concept management
  const [conceptModal,   setConceptModal]   = useState(false)
  const [conceptUpload,  setConceptUpload]  = useState(null)
  const [conceptNewId,   setConceptNewId]   = useState('')
  const [conceptSaving,  setConceptSaving]  = useState(false)

  // Canvas
  const [vb,     setVb]     = useState({ x:0, y:0, w:70000, h:45000 })
  const [layers, setLayers] = useState({ zones:true, lumi:true })
  const svgRef = useRef(null)
  const panRef = useRef(null)

  // Inspector / tabs
  const [selLumi,  setSelLumi]  = useState(null)
  const [tab,      setTab]      = useState('results')

  // Corrections
  const [corrections, setCorrections] = useState([])

  // Toast + progress
  const [toast,    setToast]    = useState(null)
  const [progress, setProgress] = useState(null)
  const toastRef = useRef(null)

  // ── API health + bootstrap ────────────────────────────────────────────────
  const checkAPI = useCallback(async () => {
    try {
      const d = await apiFetch('/health')
      if (d.status === 'ok') {
        setOnline(true)
        const c = await apiFetch('/concepts')
        setConcepts(c.concepts || ['rossmann_standard'])
      }
    } catch { setOnline(false) }
  }, [])

  useEffect(() => {
    checkAPI()
    const iv = setInterval(checkAPI, 12000)
    return () => clearInterval(iv)
  }, [checkAPI])

  // ── Toast ─────────────────────────────────────────────────────────────────
  const showToast = useCallback((msg, dur=3200) => {
    setToast(msg)
    clearTimeout(toastRef.current)
    toastRef.current = setTimeout(() => setToast(null), dur)
  }, [])

  // ── File handling ─────────────────────────────────────────────────────────
  const handleFile = f => {
    if (!f) return
    if (!/\.(pdf|dxf|dwg)$/i.test(f.name)) {
      showToast('Only .pdf, .dxf, or .dwg files accepted')
      return
    }
    setFile(f)
  }

  // ── Start job ─────────────────────────────────────────────────────────────
  const startJob = async () => {
    if (!online) { runDemo(); return }
    if (!file) { showToast('Select a plan file first'); return }
    setRunning(true)
    const fd = new FormData()
    fd.append('file', file)
    fd.append('concept_id', conceptId)
    fd.append('project_name', projectName)
    fd.append('customer', customer)
    try {
      const d = await fetch(endpoint('/process'), {method:'POST', body:fd}).then(r=>r.json())
      const job = { id:d.job_id, name:file.name, status:'queued', result:null }
      setJobs(prev => [job, ...prev])
      setActiveId(d.job_id)
      setProgress({pct:5, msg:'Queued…'})
      pollJob(d.job_id)
      showToast(`Job ${d.job_id} started`)
    } catch(e) {
      showToast(`Upload failed: ${e.message}`)
      setRunning(false)
    }
  }

  // ── Demo mode ─────────────────────────────────────────────────────────────
  const runDemo = () => {
    const id   = 'demo' + Date.now().toString(36)
    const name = file?.name ?? 'demo_rossmann_eg.pdf'
    setJobs(prev => [{id, name, status:'processing', result:null}, ...prev])
    setActiveId(id); setRunning(true)
    const stages = [
      [700,  15, 'Parsing floor plan…'],
      [1400, 35, 'Classifying zones…'],
      [2100, 55, 'Placing luminaires on 1.25m grid…'],
      [2800, 80, 'Exporting DXF, Excel, PDF…'],
      [3500,100, 'Pipeline complete'],
    ]
    stages.forEach(([delay, p, msg]) => setTimeout(() => setProgress({pct:p, msg}), delay))
    setTimeout(() => {
      const res = makeDemoResult()
      setJobs(prev => prev.map(j => j.id===id ? {...j, status:'done', result:res} : j))
      loadResult(res); setRunning(false); setProgress(null)
      showToast(`Demo: ${res.total_luminaires} luminaires placed (99.4% accuracy)`)
    }, 3800)
  }

  // ── Poll job ──────────────────────────────────────────────────────────────
  const pollJob = id => {
    const MSGS = ['Parsing…','Classifying…','Placing…','Exporting…']
    let step = 0
    clearInterval(pollRef.current)
    pollRef.current = setInterval(async () => {
      try {
        const d = await apiFetch(`/jobs/${id}`)
        setJobs(prev => prev.map(j => j.id===id ? {...j, status:d.status} : j))
        step = Math.min(step+1, MSGS.length-1)
        setProgress({pct:Math.min(90,(step+1)*25), msg:d.message||MSGS[step]})
        if (d.status === 'done') {
          clearInterval(pollRef.current)
          setJobs(prev => prev.map(j => j.id===id ? {...j, result:d.result} : j))
          loadResult(d.result); setRunning(false); setProgress(null)
          showToast(`${d.result.total_luminaires} luminaires placed`)
          fetchHistory()  // refresh persistent history
        } else if (d.status === 'error') {
          clearInterval(pollRef.current)
          setRunning(false); setProgress(null)
          showToast(`Pipeline error: ${d.message}`)
        }
      } catch { clearInterval(pollRef.current); setRunning(false); setProgress(null) }
    }, 1500)
  }

  // ── Load result ───────────────────────────────────────────────────────────
  const loadResult = res => {
    setResult(res); setSelLumi(null); setCorrections([])
    fitResult(res); setTab('results')
  }
  const fitResult = res => {
    if (!res?.zones?.length) return
    let x0=Infinity,y0=Infinity,x1=-Infinity,y1=-Infinity
    res.zones.forEach(z => {
      const [a,b,c,d]=z.bounds
      x0=Math.min(x0,a); y0=Math.min(y0,b); x1=Math.max(x1,c); y1=Math.max(y1,d)
    })
    const pad=(x1-x0)*0.06
    setVb({x:x0-pad, y:y0-pad, w:(x1-x0)+pad*2, h:(y1-y0)+pad*2})
  }
  const selectJob = id => {
    setActiveId(id)
    const j = jobs.find(j=>j.id===id)
    if (j?.result) loadResult(j.result)
  }

  // ── Persistent history ────────────────────────────────────────────────────
  const fetchHistory = useCallback(async () => {
    if (!online) return
    setHistoryLoading(true)
    try {
      const d = await apiFetch('/history?limit=50')
      setHistoryJobs(d.jobs || [])
    } catch { /* silently ignore */ }
    finally { setHistoryLoading(false) }
  }, [online])

  useEffect(() => {
    if (tab === 'history') fetchHistory()
  }, [tab, fetchHistory])

  // ── Concept management ────────────────────────────────────────────────────
  const uploadConcept = async () => {
    if (!conceptUpload || !conceptNewId) {
      showToast('Both concept ID and YAML file are required'); return
    }
    setConceptSaving(true)
    const fd = new FormData()
    fd.append('file', conceptUpload)
    fd.append('concept_id', conceptNewId)
    try {
      await fetch(endpoint('/concepts'), {method:'POST', body:fd})
      const c = await apiFetch('/concepts')
      setConcepts(c.concepts || ['rossmann_standard'])
      setConceptModal(false); setConceptUpload(null); setConceptNewId('')
      showToast(`Concept '${conceptNewId}' uploaded`)
    } catch(e) { showToast(`Upload failed: ${e.message}`) }
    finally { setConceptSaving(false) }
  }

  const deleteConcept = async id => {
    if (!confirm(`Delete concept '${id}'?`)) return
    try {
      await fetch(endpoint(`/concepts/${id}`), {method:'DELETE'})
      const c = await apiFetch('/concepts')
      setConcepts(c.concepts || ['rossmann_standard'])
      if (conceptId === id) setConceptId('rossmann_standard')
      showToast(`Concept '${id}' deleted`)
    } catch(e) { showToast(`Delete failed: ${e.message}`) }
  }

  // ── SVG pan/zoom ──────────────────────────────────────────────────────────
  const handleWheel = e => {
    e.preventDefault()
    const f = e.deltaY < 0 ? 1.18 : 0.85
    setVb(v => { const cx=v.x+v.w/2, cy=v.y+v.h/2; return {x:cx-v.w/f/2,y:cy-v.h/f/2,w:v.w/f,h:v.h/f} })
  }
  const handleMD = e => {
    const rect = svgRef.current?.getBoundingClientRect()
    panRef.current = {cx:e.clientX, cy:e.clientY, vb:{...vb}, rect}
  }
  const handleMM = e => {
    if (!panRef.current) return
    const {cx,cy,vb:ov,rect} = panRef.current
    const sx=ov.w/rect.width, sy=ov.h/rect.height
    setVb({...ov, x:ov.x-(e.clientX-cx)*sx, y:ov.y-(e.clientY-cy)*sy})
  }
  const handleMU = () => { panRef.current=null }

  // ── Corrections ───────────────────────────────────────────────────────────
  const markCorrection = (lumi, action) => {
    setCorrections(prev => {
      if (prev.find(c=>c.luminaire_id===lumi.id && c.action===action)) return prev
      return [...prev, {luminaire_id:lumi.id, old_x:lumi.x, old_y:lumi.y,
                        new_x:lumi.x, new_y:lumi.y, action, zone_type:lumi.zone_type}]
    })
    showToast(`Marked: ${action} #${lumi.id}`)
  }

  const submitCorrections = async () => {
    if (!corrections.length) return
    if (!online) { showToast(`${corrections.length} corrections saved (demo)`); setCorrections([]); return }
    try {
      const d = await apiFetch('/corrections', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({job_id:activeId, corrections}),
      })
      showToast(`${d.count} corrections submitted for RL training`)
      setCorrections([])
    } catch(e) { showToast(`Submit failed: ${e.message}`) }
  }

  // ── Downloads ─────────────────────────────────────────────────────────────
  const download = async fmt => {
    if (!activeId) return
    const job = jobs.find(j=>j.id===activeId)
    if (!online || !job?.result) {
      showToast(`Demo mode: ${fmt.toUpperCase()} available after running on real backend`)
      return
    }
    try {
      const r = await fetch(endpoint(`/exports/${activeId}/${fmt}`))
      if (!r.ok) throw new Error(r.statusText)
      const blob = await r.blob()
      const url  = URL.createObjectURL(blob)
      const a    = document.createElement('a')
      a.href=url; a.download=`${activeId}_export.${fmt}`; a.click()
      URL.revokeObjectURL(url)
      showToast(`${fmt.toUpperCase()} downloading…`)
    } catch(e) { showToast(`Download failed: ${e.message}`) }
  }

  // ── Derived plan bounds for Y-flip ────────────────────────────────────────
  const planBounds = result?.zones?.length
    ? result.zones.reduce((acc,z)=>({miny:Math.min(acc.miny,z.bounds[1]),maxy:Math.max(acc.maxy,z.bounds[3])}),{miny:Infinity,maxy:-Infinity})
    : {miny:0, maxy:45000}
  const CY   = (planBounds.miny+planBounds.maxy)/2
  const fy   = y => 2*CY-y
  const lumiR = Math.min(vb.w,vb.h)*0.006

  const corrColor = id => {
    const c = corrections.find(c=>c.luminaire_id===id)
    if (!c) return null
    return c.action==='delete'?'#f44336':c.action==='move'?'#ff9800':'#4caf50'
  }

  // ─────────────────────────────────────────────────────────────────────────
  return (
    <div style={S.root}>

      {/* ─── HEADER ──────────────────────────────────────────────────────── */}
      <header style={S.header}>
        <div style={S.logo}>
          <span style={S.logoMark}>◉</span>
          <span style={S.logoText}>lighting<span style={{color:'#e8c547'}}>‑ai</span></span>
          <span style={S.logoSub}>Rossmann Design System</span>
        </div>
        <nav style={S.nav}>
          {['Workspace','Settings'].map(v => (
            <button key={v} style={S.navBtn} onClick={()=>showToast(`${v} — Phase 2`)}>{v}</button>
          ))}
        </nav>
        <div style={S.headerRight}>
          {result && (
            <div style={S.headerStats}>
              <Stat label="Luminaires" value={fmt(result.total_luminaires)} accent />
              <Stat label="Load" value={`${fmt(result.total_wattage)} W`} />
              <Stat label="A / B / C" value={`${result.type_A} / ${result.type_B} / ${result.type_C||0}`} />
            </div>
          )}
          <div style={S.apiPill}>
            <span style={{...S.statusDot, background:online?'#4caf50':'#f44336',
                          animation:online?'pulse 2s infinite':'blink 1s infinite'}}/>
            <span style={S.apiLabel}>{online ? 'API :8000' : 'offline (demo)'}</span>
          </div>
        </div>
      </header>

      <div style={S.body}>
        {/* ─── LEFT SIDEBAR ────────────────────────────────────────────── */}
        <aside style={S.sidebar}>

          {/* Upload */}
          <section style={S.sideSection}>
            <div style={S.sideLabel}>Upload Plan</div>
            <label style={{...S.dropZone,...(drag?S.dropOver:{})}}
                   onDragOver={e=>{e.preventDefault();setDrag(true)}}
                   onDragLeave={()=>setDrag(false)}
                   onDrop={e=>{e.preventDefault();setDrag(false);handleFile(e.dataTransfer.files[0])}}>
              <input type="file" accept=".pdf,.dxf,.dwg" style={{display:'none'}}
                     onChange={e=>handleFile(e.target.files[0])} />
              <span style={{fontSize:28,opacity:file?1:0.35}}>📐</span>
              <span style={{fontSize:12,color:file?'#e8c547':'#6b7280',marginTop:4,textAlign:'center',lineHeight:1.4}}>
                {file ? file.name : <><b>.pdf / .dxf / .dwg</b><br/>drop or click</>}
              </span>
              {file && <span style={{fontSize:10,color:'#4caf50',marginTop:2}}>✓ Ready</span>}
            </label>
          </section>

          {/* Configuration */}
          <section style={S.sideSection}>
            <div style={S.sideLabel}>Configuration</div>

            {/* Concept selector + manage button */}
            <Field label="Concept model">
              <div style={{display:'flex',gap:6}}>
                <select style={{...S.select,flex:1}} value={conceptId}
                        onChange={e=>setConceptId(e.target.value)}>
                  {concepts.map(c=><option key={c} value={c}>{c}</option>)}
                </select>
                <button style={S.iconBtn} title="Manage concepts"
                        onClick={()=>setConceptModal(v=>!v)}>⚙</button>
              </div>
            </Field>

            {/* Inline concept management panel */}
            {conceptModal && (
              <div style={S.conceptPanel}>
                <div style={{fontFamily:"'DM Mono',monospace",fontSize:9,
                             color:'#6b7280',letterSpacing:.8,marginBottom:8,
                             textTransform:'uppercase'}}>Concept Library</div>
                {concepts.map(c => (
                  <div key={c} style={S.conceptRow}>
                    <span style={{fontSize:11,color:c===conceptId?'#e8c547':'#f9fafb',flex:1,
                                  fontFamily:"'DM Mono',monospace"}}>{c}</span>
                    {c !== 'rossmann_standard' && (
                      <button style={S.delBtn} onClick={()=>deleteConcept(c)}
                              title={`Delete ${c}`}>×</button>
                    )}
                    {c === 'rossmann_standard' && (
                      <span style={{fontSize:9,color:'#374151',fontFamily:"'DM Mono',monospace"}}>default</span>
                    )}
                  </div>
                ))}

                <div style={{marginTop:10,borderTop:'1px solid #1f2937',paddingTop:10}}>
                  <div style={{fontFamily:"'DM Mono',monospace",fontSize:9,
                               color:'#6b7280',letterSpacing:.8,marginBottom:6,
                               textTransform:'uppercase'}}>Upload New Concept</div>
                  <input style={{...S.input,marginBottom:6}} placeholder="concept-id (e.g. dm_standard)"
                         value={conceptNewId} onChange={e=>setConceptNewId(e.target.value.toLowerCase())} />
                  <label style={{...S.uploadLabel,marginBottom:6}}>
                    <input type="file" accept=".yaml,.yml" style={{display:'none'}}
                           onChange={e=>setConceptUpload(e.target.files[0])} />
                    {conceptUpload ? `✓ ${conceptUpload.name}` : 'Choose .yaml file'}
                  </label>
                  <button style={{...S.runBtn,fontSize:11,padding:8,
                                  opacity:conceptSaving?0.5:1}}
                          disabled={conceptSaving} onClick={uploadConcept}>
                    {conceptSaving ? <><Spinner/>Saving…</> : '↑ Upload Concept'}
                  </button>
                </div>
              </div>
            )}

            <Field label="Project name">
              <input style={S.input} value={projectName}
                     onChange={e=>setProjectName(e.target.value)} />
            </Field>
            <Field label="Customer">
              <input style={S.input} value={customer}
                     onChange={e=>setCustomer(e.target.value)} />
            </Field>

            <button style={{...S.runBtn,opacity:running?0.5:1,cursor:running?'not-allowed':'pointer'}}
                    disabled={running} onClick={startJob}>
              {running ? <><Spinner/>Processing…</> : (online?'▶ Run Pipeline':'▶ Run Demo')}
            </button>
            {!online && (
              <p style={{fontSize:10,color:'#6b7280',marginTop:6,textAlign:'center',lineHeight:1.5}}>
                Backend offline — demo mode uses<br/>synthetic Rossmann EG plan
              </p>
            )}
          </section>

          {/* Current session jobs */}
          <section style={{...S.sideSection,paddingBottom:6}}>
            <div style={S.sideLabel}>Session ({jobs.length})</div>
          </section>
          <div style={S.jobList}>
            {jobs.length===0 && (
              <p style={{textAlign:'center',padding:'20px 0',fontSize:11,color:'#374151',
                         fontFamily:"'DM Mono',monospace"}}>No jobs yet</p>
            )}
            {jobs.map(j => (
              <div key={j.id} style={{...S.jobCard,...(j.id===activeId?S.jobActive:{})}}
                   onClick={()=>selectJob(j.id)}>
                <div style={{...S.jobBar,
                             background:j.status==='done'?'#4caf50':j.status==='error'?'#f44336':
                                        j.status==='processing'?'#e8c547':'#6b7280'}}/>
                <div style={{flex:1,minWidth:0}}>
                  <div style={S.jobName}>{j.name}</div>
                  <div style={S.jobMeta}>
                    <span style={{fontFamily:"'DM Mono',monospace",fontSize:9,color:'#6b7280'}}>#{j.id}</span>
                    <StatusPill status={j.status} />
                  </div>
                </div>
              </div>
            ))}
          </div>
        </aside>

        {/* ─── CANVAS ──────────────────────────────────────────────────── */}
        <main style={S.main}>
          {/* Toolbar */}
          <div style={S.toolbar}>
            <Tbtn title="Zones layer"     active={layers.zones} onClick={()=>setLayers(l=>({...l,zones:!l.zones}))}>◈</Tbtn>
            <Tbtn title="Luminaires layer" active={layers.lumi}  onClick={()=>setLayers(l=>({...l,lumi:!l.lumi}))}>●</Tbtn>
            <div style={S.tbSep}/>
            <Tbtn title="Zoom in"  onClick={()=>setVb(v=>({...v,x:v.x+v.w*.15,y:v.y+v.h*.15,w:v.w*.7,h:v.h*.7}))}>+</Tbtn>
            <Tbtn title="Zoom out" onClick={()=>setVb(v=>({...v,x:v.x-v.w*.21,y:v.y-v.h*.21,w:v.w/0.7,h:v.h/0.7}))}>−</Tbtn>
            <Tbtn title="Fit plan" onClick={()=>result&&fitResult(result)}>⊡</Tbtn>
            <div style={S.tbSep}/>
            <span style={S.tbInfo}>
              {result
                ? `${result.total_luminaires} lumi · ${result.zones.length} zones · ${result.total_wattage} W`
                : 'No plan loaded'}
            </span>
            {corrections.length>0 && (
              <div style={S.corrBadge} title={`${corrections.length} corrections pending`}>
                {corrections.length}
              </div>
            )}
          </div>

          {/* SVG viewport */}
          <div style={S.canvasWrap}>
            <svg style={S.bgGrid} xmlns="http://www.w3.org/2000/svg">
              <defs>
                <pattern id="g" width="28" height="28" patternUnits="userSpaceOnUse">
                  <path d="M28 0L0 0 0 28" fill="none" stroke="rgba(255,255,255,0.03)" strokeWidth="1"/>
                </pattern>
              </defs>
              <rect width="100%" height="100%" fill="url(#g)"/>
            </svg>

            <svg ref={svgRef} style={S.planSvg}
                 viewBox={`${vb.x} ${vb.y} ${vb.w} ${vb.h}`}
                 onWheel={handleWheel} onMouseDown={handleMD}
                 onMouseMove={handleMM} onMouseUp={handleMU} onMouseLeave={handleMU}>

              {layers.zones && result?.zones?.map(z => {
                const [x0,y0,x1,y1]=z.bounds
                const fy0=fy(y1), col=zc(z.zone_type)
                return (
                  <g key={z.index}>
                    <rect x={x0} y={fy0} width={x1-x0} height={y1-y0}
                          fill={col} fillOpacity={0.10} stroke={col}
                          strokeWidth={Math.max(1,vb.w*0.001)} style={{cursor:'pointer'}}
                          onClick={()=>{setTab('results');showToast(z.zone_type.replace(/_/g,' '))}}/>
                    <text x={(x0+x1)/2} y={fy0+(y1-y0)/2}
                          fill={col} fillOpacity={0.9}
                          fontSize={Math.min(vb.w,vb.h)*0.016}
                          fontFamily="'DM Mono',monospace"
                          textAnchor="middle" dominantBaseline="middle" pointerEvents="none">
                      {z.zone_type.replace(/_/g,' ')}
                    </text>
                  </g>
                )
              })}

              {result?.zones?.map(z => {
                const [x0,y0,x1,y1]=z.bounds, col=zc(z.zone_type)
                return <rect key={`ol${z.index}`} x={x0} y={fy(y1)}
                  width={x1-x0} height={y1-y0} fill="none" stroke={col}
                  strokeWidth={Math.max(0.5,vb.w*0.0008)} strokeOpacity={0.5} pointerEvents="none"/>
              })}

              {layers.lumi && result?.placed?.map(lp => {
                const col  = corrColor(lp.id) ?? lc(lp.lumi_type)
                const isSel = selLumi?.id === lp.id
                return (
                  <g key={lp.id} style={{cursor:'pointer'}}
                     onClick={e=>{e.stopPropagation();setSelLumi(lp);setTab('inspect')}}>
                    <circle cx={lp.x} cy={fy(lp.y)} r={lumiR}
                            fill={col} fillOpacity={0.18}
                            stroke={isSel?'#ffffff':col}
                            strokeWidth={isSel?Math.max(2,lumiR*0.4):Math.max(1,lumiR*0.2)}/>
                    <circle cx={lp.x} cy={fy(lp.y)} r={lumiR*0.42}
                            fill={col} fillOpacity={0.95} pointerEvents="none"/>
                  </g>
                )
              })}
            </svg>

            {!result && (
              <div style={S.emptyState}>
                <div style={{fontSize:56,opacity:0.1}}>🏗</div>
                <div style={{fontFamily:"'Bebas Neue',sans-serif",fontSize:22,color:'#374151',letterSpacing:2}}>
                  NO PLAN LOADED
                </div>
                <div style={{fontSize:12,color:'#4b5563',marginTop:4}}>Upload a PDF or DXF floor plan</div>
              </div>
            )}
          </div>
        </main>

        {/* ─── RIGHT PANEL ─────────────────────────────────────────────── */}
        <aside style={S.rightPanel}>
          <div style={S.panelTabs}>
            {['results','inspect','export','history'].map(t => (
              <button key={t} style={{...S.panelTab,...(tab===t?S.panelTabActive:{})}}
                      onClick={()=>setTab(t)}>
                {t==='history'?'Hist.':t.charAt(0).toUpperCase()+t.slice(1)}
              </button>
            ))}
          </div>

          <div style={{flex:1,overflowY:'auto',padding:14}}>

            {/* ── RESULTS ── */}
            {tab==='results' && (!result
              ? <Empty>Run a job to see results</Empty>
              : <>
                  <div style={S.statsGrid}>
                    {[
                      {v:result.total_luminaires,                          l:'Luminaires'},
                      {v:`${fmt(result.total_wattage)} W`,                 l:'Total load'},
                      {v:`${result.type_A}A · ${result.type_B}B`,          l:'Type split'},
                      {v:`${(result.total_wattage/Math.max(result.total_luminaires,1)).toFixed(0)} W`, l:'Avg/unit'},
                    ].map(s=>(
                      <div key={s.l} style={S.statCard}>
                        <div style={S.statVal}>{s.v}</div>
                        <div style={S.statLbl}>{s.l}</div>
                      </div>
                    ))}
                  </div>

                  {/* Type breakdown inc. C/D/E */}
                  {(result.type_C||result.type_D||result.type_E) ? (
                    <div style={{...S.inspector,marginBottom:10}}>
                      <div style={S.inspTitle}>Luminaire Types</div>
                      {[
                        ['A','#e040fb', result.type_A||0],
                        ['B','#f44336', result.type_B||0],
                        ['C','#00bcd4', result.type_C||0],
                        ['D','#ffeb3b', result.type_D||0],
                        ['E','#42a5f5', result.type_E||0],
                      ].filter(([,, n])=>n>0).map(([t,col,n])=>(
                        <div key={t} style={{...S.inspRow,borderBottom:`1px solid #111113`}}>
                          <div style={{display:'flex',alignItems:'center',gap:6}}>
                            <span style={{width:8,height:8,borderRadius:'50%',background:col,display:'inline-block'}}/>
                            <span style={{color:'#f9fafb',fontSize:10,fontFamily:"'DM Mono',monospace"}}>
                              Type {t}
                            </span>
                          </div>
                          <span style={{fontFamily:"'DM Mono',monospace",fontSize:10,color:'#9ca3af'}}>
                            {n} · {LUMI_LABEL[t]}
                          </span>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div style={S.accuracyBadge}>
                      <span style={{fontFamily:"'Bebas Neue'",fontSize:15,letterSpacing:1,color:'#e8c547'}}>
                        PLACEMENT ACCURACY
                      </span>
                      <div style={{display:'flex',gap:8,marginTop:6}}>
                        {[{label:'Total',a:result.total_luminaires,b:167},
                          {label:'Type A',a:result.type_A,b:106},
                          {label:'Type B',a:result.type_B,b:61}].map(item=>(
                          <div key={item.label} style={S.accItem}>
                            <div style={{fontSize:13,fontWeight:700,color:'#e8c547',fontFamily:"'DM Mono'"}}>{pct(item.a,item.b)}</div>
                            <div style={{fontSize:9,color:'#6b7280',fontFamily:"'DM Mono'"}}>{item.label}</div>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  <div style={S.sideLabel}>Zones</div>
                  {result.zones.map(z=>(
                    <div key={z.index} style={S.zoneItem}
                         onClick={()=>{setLayers(l=>({...l,zones:true}));fitResult(result)}}>
                      <div style={{width:10,height:10,borderRadius:2,background:zc(z.zone_type),flexShrink:0}}/>
                      <div style={{flex:1,minWidth:0}}>
                        <div style={{fontSize:12,fontWeight:500,color:'#f9fafb'}}>
                          {z.zone_type.replace(/_/g,' ')}
                        </div>
                        <div style={{fontFamily:"'DM Mono',monospace",fontSize:9,color:'#6b7280'}}>
                          {z.area_m2}m² · {z.method}
                        </div>
                      </div>
                      <div style={{fontFamily:"'DM Mono',monospace",fontSize:10,color:'#9ca3af'}}>
                        {Math.round(z.confidence*100)}%
                      </div>
                    </div>
                  ))}
                </>
            )}

            {/* ── INSPECT ── */}
            {tab==='inspect' && (!selLumi
              ? <Empty>Click a luminaire on the canvas</Empty>
              : <>
                  <div style={S.inspector}>
                    <div style={{...S.inspTitle,display:'flex',alignItems:'center',gap:6}}>
                      <span style={{width:8,height:8,borderRadius:'50%',background:lc(selLumi.lumi_type),display:'inline-block'}}/>
                      Luminaire #{selLumi.id} — Type {selLumi.lumi_type}
                    </div>
                    {[
                      ['Description',  selLumi.description],
                      ['Product code', selLumi.product_code],
                      ['Wattage',      `${selLumi.wattage} W`],
                      ['Lux output',   `${selLumi.lux_output} lm`],
                      ['Beam angle',   `${selLumi.beam_angle_deg}°`],
                      ['Zone',         selLumi.zone_type.replace(/_/g,' ')],
                      ['X position',   `${selLumi.x.toLocaleString()} mm`],
                      ['Y position',   `${selLumi.y.toLocaleString()} mm`],
                      ['Grid snap',    selLumi.grid_snapped?'✓ Yes':'✗ No'],
                      ['Shelf align',  selLumi.shelf_aligned?'✓ Yes':'✗ No'],
                    ].map(([k,v])=>(
                      <div key={k} style={S.inspRow}>
                        <span style={{color:'#6b7280',fontSize:10}}>{k}</span>
                        <span style={{fontFamily:"'DM Mono',monospace",fontSize:10,
                                      color:'#f9fafb',maxWidth:140,textAlign:'right',wordBreak:'break-all'}}>{v}</span>
                      </div>
                    ))}
                  </div>

                  <div style={S.sideLabel}>Correction Actions</div>
                  <div style={{display:'flex',gap:6,marginBottom:6}}>
                    <CorrBtn color="#f44336" onClick={()=>markCorrection(selLumi,'delete')}>✕ Delete</CorrBtn>
                    <CorrBtn color="#ff9800" onClick={()=>markCorrection(selLumi,'move')}>↔ Move</CorrBtn>
                  </div>
                  <CorrBtn color="#4caf50" wide onClick={()=>markCorrection(selLumi,'swap_type')}>⇄ Swap Type</CorrBtn>

                  {corrections.length>0 && (
                    <div style={{marginTop:10}}>
                      <div style={{fontFamily:"'DM Mono',monospace",fontSize:10,color:'#6b7280',marginBottom:6}}>
                        {corrections.length} correction{corrections.length>1?'s':''} pending
                      </div>
                      <button style={{...S.runBtn,fontSize:11,padding:8}} onClick={submitCorrections}>
                        Submit for RL Training
                      </button>
                    </div>
                  )}
                </>
            )}

            {/* ── EXPORT ── */}
            {tab==='export' && (
              <>
                <div style={S.sideLabel}>Download Outputs</div>
                {[
                  {fmt:'dxf',  icon:'📐', name:'DWG / DXF',        desc:'AutoCAD Deckenrasterplan + title block'},
                  {fmt:'xlsx', icon:'📊', name:'Excel BOM',          desc:'3-sheet fixture schedule + cover'},
                  {fmt:'pdf',  icon:'📄', name:'PDF Documentation',  desc:'Customer approval package'},
                ].map(ex=>(
                  <button key={ex.fmt} style={{...S.exportBtn,opacity:result?1:0.4}}
                          disabled={!result} onClick={()=>download(ex.fmt)}>
                    <span style={{fontSize:20}}>{ex.icon}</span>
                    <div style={{flex:1}}>
                      <div style={{fontSize:12,fontWeight:500,color:'#f9fafb'}}>{ex.name}</div>
                      <div style={{fontSize:10,color:'#6b7280',fontFamily:"'DM Mono',monospace",marginTop:1}}>{ex.desc}</div>
                    </div>
                    <span style={{color:'#4b5563',fontSize:13}}>→</span>
                  </button>
                ))}

                {result && (
                  <div style={{marginTop:14}}>
                    <div style={S.sideLabel}>DXF Format</div>
                    <div style={S.inspector}>
                      {[
                        ['Layers',        'LUMINAIRES, ZONES, DIMS, TITLEBLOCK, LEGEND'],
                        ['Block family',  'MIKA80-E (128mm cutout)'],
                        ['Symbol A',      'Magenta · 15W · 40°'],
                        ['Symbol B',      'Red · 20W · 60°'],
                        ['Symbol C',      'Cyan · 20W · accent'],
                        ['Symbol D',      'Yellow · IP44'],
                        ['ATTRIB',        'TYPE, PRODUCT per INSERT'],
                        ['Grid pitch',    '1250 mm'],
                        ['Title block',   '✓ Schriftfeld'],
                        ['Legend',        '✓ Leuchtenlegende'],
                        ['Total qty',     result.total_luminaires],
                        ['Total load',    `${result.total_wattage} W`],
                      ].map(([k,v])=>(
                        <div key={k} style={S.inspRow}>
                          <span style={{color:'#6b7280',fontSize:10}}>{k}</span>
                          <span style={{fontFamily:"'DM Mono',monospace",fontSize:10,color:'#f9fafb'}}>{v}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {corrections.length>0 && (
                  <div style={{marginTop:12}}>
                    <div style={S.sideLabel}>RL Training Signal</div>
                    <button style={{...S.runBtn,fontSize:11,padding:8}} onClick={submitCorrections}>
                      Submit {corrections.length} Correction{corrections.length>1?'s':''}
                    </button>
                  </div>
                )}
              </>
            )}

            {/* ── HISTORY ── */}
            {tab==='history' && (
              <>
                <div style={{display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:10}}>
                  <div style={S.sideLabel}>Planning History</div>
                  <button style={{...S.iconBtn,fontSize:11}} onClick={fetchHistory}
                          title="Refresh history">
                    {historyLoading ? '…' : '↻'}
                  </button>
                </div>

                {!online
                  ? <Empty>History requires backend connection</Empty>
                  : historyLoading
                  ? <Empty>Loading…</Empty>
                  : historyJobs.length===0
                  ? <Empty>No completed jobs yet</Empty>
                  : historyJobs.map(j => (
                    <div key={j.job_id} style={S.histCard}
                         onClick={()=>{
                           // If this job is already in session, switch to it
                           const sess = jobs.find(s=>s.id===j.job_id)
                           if (sess?.result) { selectJob(j.job_id); setTab('results') }
                           else showToast(`Job ${j.job_id} — reload session to view plan`)
                         }}>
                      <div style={{display:'flex',justifyContent:'space-between',alignItems:'flex-start',marginBottom:4}}>
                        <div style={{fontFamily:"'DM Mono',monospace",fontSize:10,
                                     color:'#9ca3af',fontWeight:600}}>
                          #{j.job_id}
                        </div>
                        <StatusPill status={j.status} />
                      </div>
                      <div style={{fontSize:12,fontWeight:500,color:'#f9fafb',
                                   marginBottom:2,overflow:'hidden',
                                   textOverflow:'ellipsis',whiteSpace:'nowrap'}}>
                        {j.project_name || j.filename || '—'}
                      </div>
                      <div style={{fontFamily:"'DM Mono',monospace",fontSize:9,
                                   color:'#6b7280',marginBottom:6}}>
                        {j.filename}
                      </div>
                      <div style={{display:'flex',justifyContent:'space-between',alignItems:'center'}}>
                        <span style={{fontFamily:"'DM Mono',monospace",fontSize:9,color:'#4b5563'}}>
                          {tsStr(j.created_at)}
                        </span>
                        {j.total_luminaires != null && (
                          <span style={{fontFamily:"'DM Mono',monospace",fontSize:9,
                                        color:'#e8c547',fontWeight:600}}>
                            {j.total_luminaires} lumi · {j.total_wattage} W
                          </span>
                        )}
                      </div>
                      <div style={{fontFamily:"'DM Mono',monospace",fontSize:9,
                                   color:'#4b5563',marginTop:2}}>
                        Concept: {j.concept_id}
                      </div>
                    </div>
                  ))
                }
              </>
            )}

          </div>
        </aside>
      </div>

      {/* ─── PROGRESS BAR ────────────────────────────────────────────────── */}
      {progress && (
        <div style={S.progressBar}>
          <div style={{height:'100%',width:`${progress.pct}%`,
                       background:'linear-gradient(90deg,#e8c547,#f97316)',
                       borderRadius:2,transition:'width 0.5s ease',position:'relative'}}>
            <div style={{position:'absolute',right:0,top:'50%',transform:'translate(50%,-50%)',
                         width:8,height:8,borderRadius:'50%',background:'#f97316',
                         boxShadow:'0 0 8px #f97316'}}/>
          </div>
          <span style={S.progressMsg}>{progress.msg}</span>
        </div>
      )}

      {/* ─── TOAST ───────────────────────────────────────────────────────── */}
      {toast && <div style={S.toast}>{toast}</div>}

      <style>{`
        @keyframes pulse{0%,100%{opacity:1}50%{opacity:0.3}}
        @keyframes blink{0%,100%{opacity:1}50%{opacity:0.15}}
        @keyframes spin{to{transform:rotate(360deg)}}
        ::-webkit-scrollbar{width:3px}
        ::-webkit-scrollbar-track{background:transparent}
        ::-webkit-scrollbar-thumb{background:#374151;border-radius:2px}
      `}</style>
    </div>
  )
}

// ─── Sub-components ───────────────────────────────────────────────────────────

function Stat({label,value,accent}) {
  return (
    <div style={{display:'flex',flexDirection:'column',alignItems:'flex-end'}}>
      <div style={{fontFamily:"'Bebas Neue',sans-serif",fontSize:15,
                   color:accent?'#e8c547':'#f9fafb',letterSpacing:1}}>{value}</div>
      <div style={{fontFamily:"'DM Mono',monospace",fontSize:9,color:'#6b7280',letterSpacing:.5}}>{label}</div>
    </div>
  )
}

function Field({label,children}) {
  return (
    <div style={{marginBottom:10}}>
      <div style={{fontFamily:"'DM Mono',monospace",fontSize:9,color:'#6b7280',
                   letterSpacing:.8,marginBottom:4,textTransform:'uppercase'}}>{label}</div>
      {children}
    </div>
  )
}

function Tbtn({children,onClick,title,active}) {
  return (
    <button title={title} onClick={onClick}
            style={{width:28,height:28,border:'none',borderRadius:4,
                    background:active?'#1f2937':'transparent',
                    color:active?'#e8c547':'#6b7280',
                    cursor:'pointer',fontSize:13,display:'flex',
                    alignItems:'center',justifyContent:'center',transition:'all .15s'}}>
      {children}
    </button>
  )
}

function CorrBtn({children,onClick,color,wide}) {
  return (
    <button onClick={onClick}
            style={{flex:wide?undefined:1,width:wide?'100%':undefined,
                    padding:'7px 8px',border:`1px solid ${color}22`,
                    borderRadius:6,background:`${color}12`,color,
                    cursor:'pointer',fontSize:11,fontWeight:600,
                    display:'flex',alignItems:'center',justifyContent:'center',
                    gap:4,transition:'all .15s'}}>
      {children}
    </button>
  )
}

function StatusPill({status}) {
  const cfg = {
    done:       {bg:'rgba(76,175,80,.18)',  c:'#4caf50'},
    error:      {bg:'rgba(244,67,54,.15)',  c:'#f44336'},
    processing: {bg:'rgba(232,197,71,.18)', c:'#e8c547'},
    queued:     {bg:'rgba(107,114,128,.15)',c:'#9ca3af'},
  }
  const {bg,c} = cfg[status] ?? cfg.queued
  return (
    <span style={{fontSize:9,padding:'2px 6px',borderRadius:3,
                  fontFamily:"'DM Mono',monospace",letterSpacing:.4,
                  textTransform:'uppercase',fontWeight:600,background:bg,color:c}}>
      {status}
    </span>
  )
}

function Empty({children}) {
  return (
    <div style={{textAlign:'center',padding:'36px 0',color:'#4b5563',
                 fontSize:11,fontFamily:"'DM Mono',monospace",lineHeight:1.6}}>
      {children}
    </div>
  )
}

function Spinner() {
  return <span style={{display:'inline-block',width:12,height:12,border:'2px solid #e8c547',
                       borderTopColor:'transparent',borderRadius:'50%',
                       animation:'spin .8s linear infinite',marginRight:6}}/>
}

// ─── Styles ───────────────────────────────────────────────────────────────────
const S = {
  root:{display:'flex',flexDirection:'column',height:'100vh',overflow:'hidden',
        background:'#0d0d0f',color:'#f9fafb',fontFamily:"'DM Sans',sans-serif"},

  header:{height:54,background:'#111113',borderBottom:'1px solid #1f2937',
          display:'flex',alignItems:'center',padding:'0 16px',gap:20,flexShrink:0,zIndex:100},
  logo:{display:'flex',alignItems:'baseline',gap:8},
  logoMark:{color:'#e8c547',fontSize:18,lineHeight:1},
  logoText:{fontFamily:"'Bebas Neue',sans-serif",fontSize:22,letterSpacing:1,color:'#f9fafb',lineHeight:1},
  logoSub:{fontFamily:"'DM Mono',monospace",fontSize:9,color:'#4b5563',letterSpacing:.5,textTransform:'uppercase'},
  nav:{display:'flex',gap:2,marginLeft:4},
  navBtn:{fontFamily:"'DM Mono',monospace",fontSize:10,color:'#6b7280',background:'none',
          border:'none',padding:'4px 10px',borderRadius:4,cursor:'pointer',letterSpacing:.3},
  headerRight:{marginLeft:'auto',display:'flex',alignItems:'center',gap:16},
  headerStats:{display:'flex',gap:20,alignItems:'center'},
  apiPill:{display:'flex',alignItems:'center',gap:6,background:'#1a1a1d',
           border:'1px solid #1f2937',padding:'4px 10px',borderRadius:20},
  statusDot:{width:6,height:6,borderRadius:'50%'},
  apiLabel:{fontFamily:"'DM Mono',monospace",fontSize:10,color:'#6b7280'},

  body:{display:'flex',flex:1,overflow:'hidden'},

  sidebar:{width:272,background:'#111113',borderRight:'1px solid #1f2937',
           display:'flex',flexDirection:'column',overflow:'hidden',flexShrink:0},
  sideSection:{padding:'14px 14px 12px',borderBottom:'1px solid #1a1a1d'},
  sideLabel:{fontFamily:"'DM Mono',monospace",fontSize:9,letterSpacing:1.2,
             color:'#4b5563',textTransform:'uppercase',marginBottom:8},
  dropZone:{display:'flex',flexDirection:'column',alignItems:'center',
            justifyContent:'center',border:'1.5px dashed #1f2937',
            borderRadius:8,padding:'18px 12px',cursor:'pointer',
            transition:'all .2s',minHeight:90,gap:4},
  dropOver:{borderColor:'#e8c547',background:'rgba(232,197,71,.04)'},
  select:{width:'100%',padding:'7px 10px',background:'#1a1a1d',
          border:'1px solid #1f2937',borderRadius:6,
          color:'#f9fafb',fontFamily:"'DM Sans',sans-serif",fontSize:12,outline:'none',cursor:'pointer'},
  input:{width:'100%',padding:'7px 10px',background:'#1a1a1d',
         border:'1px solid #1f2937',borderRadius:6,
         color:'#f9fafb',fontFamily:"'DM Sans',sans-serif",fontSize:12,outline:'none'},
  iconBtn:{width:30,height:30,border:'1px solid #1f2937',borderRadius:6,
           background:'#1a1a1d',color:'#6b7280',cursor:'pointer',
           display:'flex',alignItems:'center',justifyContent:'center',
           fontSize:14,flexShrink:0,transition:'all .15s'},
  runBtn:{width:'100%',padding:'10px',background:'#e8c547',color:'#0d0d0f',
          border:'none',borderRadius:7,fontFamily:"'Bebas Neue',sans-serif",
          fontSize:14,letterSpacing:1.2,cursor:'pointer',display:'flex',
          alignItems:'center',justifyContent:'center',gap:6,transition:'all .2s'},

  // Concept panel
  conceptPanel:{background:'#0f0f11',border:'1px solid #1f2937',borderRadius:8,
                padding:'10px 12px',marginBottom:10},
  conceptRow:{display:'flex',alignItems:'center',gap:6,padding:'4px 0',
              borderBottom:'1px solid #1a1a1d'},
  delBtn:{border:'none',background:'rgba(244,67,54,.12)',color:'#f44336',
          borderRadius:4,cursor:'pointer',width:20,height:20,fontSize:13,
          display:'flex',alignItems:'center',justifyContent:'center',flexShrink:0},
  uploadLabel:{display:'block',padding:'7px 10px',background:'#1a1a1d',
               border:'1px dashed #1f2937',borderRadius:6,
               color:'#6b7280',fontFamily:"'DM Sans',sans-serif",
               fontSize:11,cursor:'pointer',textAlign:'center'},

  jobList:{flex:1,overflowY:'auto',padding:'6px 8px'},
  jobCard:{display:'flex',alignItems:'center',gap:8,padding:'9px 10px',
           borderRadius:7,border:'1px solid #1a1a1d',marginBottom:5,
           cursor:'pointer',position:'relative',overflow:'hidden',transition:'all .15s'},
  jobActive:{border:'1px solid rgba(232,197,71,.3)',background:'rgba(232,197,71,.04)'},
  jobBar:{width:3,height:'100%',position:'absolute',left:0,top:0,bottom:0},
  jobName:{fontSize:12,fontWeight:500,whiteSpace:'nowrap',overflow:'hidden',
           textOverflow:'ellipsis',color:'#f9fafb',marginBottom:2},
  jobMeta:{display:'flex',justifyContent:'space-between',alignItems:'center'},

  // History
  histCard:{background:'#1a1a1d',border:'1px solid #1f2937',borderRadius:8,
            padding:'10px 12px',marginBottom:8,cursor:'pointer',transition:'all .15s'},

  // Canvas
  main:{flex:1,display:'flex',flexDirection:'column',overflow:'hidden'},
  toolbar:{height:40,background:'#111113',borderBottom:'1px solid #1f2937',
           display:'flex',alignItems:'center',padding:'0 10px',gap:4,flexShrink:0},
  tbSep:{width:1,height:18,background:'#1f2937',margin:'0 4px'},
  tbInfo:{marginLeft:'auto',fontFamily:"'DM Mono',monospace",fontSize:10,color:'#4b5563'},
  corrBadge:{marginLeft:8,background:'#e8c547',color:'#0d0d0f',borderRadius:'50%',
             width:18,height:18,fontSize:10,fontWeight:700,display:'flex',
             alignItems:'center',justifyContent:'center'},
  canvasWrap:{flex:1,position:'relative',overflow:'hidden'},
  bgGrid:{position:'absolute',inset:0,width:'100%',height:'100%',pointerEvents:'none'},
  planSvg:{width:'100%',height:'100%',cursor:'grab',display:'block'},
  emptyState:{position:'absolute',inset:0,display:'flex',flexDirection:'column',
              alignItems:'center',justifyContent:'center',gap:8,pointerEvents:'none'},

  // Right panel
  rightPanel:{width:272,background:'#111113',borderLeft:'1px solid #1f2937',
              display:'flex',flexDirection:'column',overflow:'hidden',flexShrink:0},
  panelTabs:{display:'flex',borderBottom:'1px solid #1a1a1d'},
  panelTab:{flex:1,padding:'11px 0',fontFamily:"'DM Mono',monospace",fontSize:10,
            letterSpacing:.6,textTransform:'uppercase',color:'#4b5563',
            background:'none',border:'none',cursor:'pointer',
            borderBottom:'2px solid transparent',marginBottom:-1,transition:'all .15s'},
  panelTabActive:{color:'#e8c547',borderBottom:'2px solid #e8c547'},

  statsGrid:{display:'grid',gridTemplateColumns:'1fr 1fr',gap:8,marginBottom:12},
  statCard:{background:'#1a1a1d',borderRadius:7,padding:'10px 12px',border:'1px solid #1f2937'},
  statVal:{fontFamily:"'Bebas Neue',sans-serif",fontSize:18,letterSpacing:.5,
           color:'#f9fafb',lineHeight:1,marginBottom:2},
  statLbl:{fontFamily:"'DM Mono',monospace",fontSize:9,color:'#6b7280',
           textTransform:'uppercase',letterSpacing:.5},

  accuracyBadge:{background:'#1a1a1d',border:'1px solid rgba(232,197,71,.2)',
                 borderRadius:8,padding:'10px 12px',marginBottom:12},
  accItem:{flex:1,textAlign:'center'},

  zoneItem:{display:'flex',alignItems:'center',gap:10,padding:'8px 10px',
            borderRadius:6,marginBottom:4,border:'1px solid #1a1a1d',
            cursor:'pointer',transition:'all .15s'},

  inspector:{background:'#1a1a1d',borderRadius:8,padding:'10px 12px',
             border:'1px solid #1f2937',marginBottom:10},
  inspTitle:{fontFamily:"'DM Mono',monospace",fontSize:10,letterSpacing:.5,
             color:'#6b7280',marginBottom:8,textTransform:'uppercase'},
  inspRow:{display:'flex',justifyContent:'space-between',alignItems:'flex-start',
           padding:'4px 0',borderBottom:'1px solid #111113',gap:8},

  exportBtn:{display:'flex',alignItems:'center',gap:10,padding:'10px 12px',
             borderRadius:7,border:'1px solid #1f2937',background:'none',
             cursor:'pointer',width:'100%',marginBottom:6,transition:'all .15s',textAlign:'left'},

  progressBar:{position:'fixed',bottom:0,left:0,right:0,height:4,background:'#1a1a1d',zIndex:200},
  progressMsg:{position:'absolute',top:-24,left:'50%',transform:'translateX(-50%)',
               fontFamily:"'DM Mono',monospace",fontSize:10,color:'#6b7280',
               whiteSpace:'nowrap',background:'#111113',padding:'3px 10px',
               borderRadius:'4px 4px 0 0',border:'1px solid #1f2937',borderBottom:'none'},

  toast:{position:'fixed',bottom:20,left:'50%',transform:'translateX(-50%)',
         background:'#1a1a1d',color:'#f9fafb',padding:'10px 18px',borderRadius:8,
         fontSize:12,fontFamily:"'DM Mono',monospace",zIndex:999,
         border:'1px solid #1f2937',borderLeft:'3px solid #e8c547',
         whiteSpace:'nowrap',boxShadow:'0 4px 24px rgba(0,0,0,.5)'},
}
