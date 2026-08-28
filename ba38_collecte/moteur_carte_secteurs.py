#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Génération de la carte HTML interactive des secteurs de collecte BAI 38.

Version allégée de dev/uploads/collecte_fichiers_source/generer_carte_secteurs.py :
le script d'origine recalculait les secteurs depuis liste-magasins2026.xlsx (soit
via une logique Grenoble-only, soit en extrayant par regex les regroupements
codés en dur dans generer_tournees_bai.py — un mécanisme fragile). Ici, on
réutilise directement le référentiel magasins (colonnes Nom/Latitude/Longitude/
Secteur) déjà calculé par collecte_moteur_tournees.lire_magasins() pour la même
génération, qui applique déjà tous les regroupements (Gresivaudan, Voiron,
Rives, Seyssinet...) — pas de logique de secteur dupliquée ni de parsing de
script tiers.

generer_carte_secteurs(data, output_path) où data = liste de dicts
{Nom, Latitude, Longitude, Secteur} — voir calculer_polygones()/generer_html()
ci-dessous, copiées quasi telles quelles du script d'origine.
"""
import json
from collections import defaultdict

import numpy as np
from scipy.spatial import ConvexHull

from ba38_utilitaires.organisation import get_organisation


def calculer_polygones(data, margin=0.008):
    groupes = defaultdict(list)
    for m in data:
        groupes[m['Secteur']].append([m['Latitude'], m['Longitude']])

    polygones = {}
    for sec, pts in groupes.items():
        arr = np.array(pts)
        if len(pts) == 1:
            lat, lon = pts[0]
            r = 0.012
            import math
            poly = [[lat + r * math.cos(a), lon + r * math.sin(a)]
                    for a in [i * 2 * math.pi / 8 for i in range(8)]]
        elif len(pts) == 2:
            lat1, lon1 = pts[0]; lat2, lon2 = pts[1]; m_val = 0.010
            poly = [
                [min(lat1, lat2) - m_val, min(lon1, lon2) - m_val],
                [max(lat1, lat2) + m_val, min(lon1, lon2) - m_val],
                [max(lat1, lat2) + m_val, max(lon1, lon2) + m_val],
                [min(lat1, lat2) - m_val, max(lon1, lon2) + m_val],
            ]
        else:
            center = arr.mean(axis=0)
            expanded = []
            for p in arr:
                d = p - center
                n = np.linalg.norm(d)
                expanded.append((p + d / n * margin).tolist() if n > 0 else p.tolist())
            expanded = np.array(expanded)
            try:
                hull = ConvexHull(expanded)
                poly = expanded[hull.vertices].tolist()
            except Exception:
                poly = arr.tolist()
        polygones[sec] = poly

    return polygones


def generer_html(data, polygones, output_path, annee):
    nb_mag = len(data)
    nb_sec = len(polygones)
    adresse_siege = get_organisation()["adresse"].replace("\n", ", ")

    magasins_json = json.dumps(data, ensure_ascii=False)
    polygones_json = json.dumps(polygones, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Carte des Secteurs — BAI 38 Collecte {annee}</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:'Segoe UI',sans-serif; background:#f5f5f5; color:#222; height:100vh; display:flex; flex-direction:column; }}
  header {{ background:#1F4E79; padding:10px 20px; display:flex; align-items:center; gap:12px; border-bottom:2px solid #0f3460; flex-wrap:wrap; }}
  header h1 {{ font-size:1rem; font-weight:600; color:#fff; letter-spacing:1px; }}
  header span {{ font-size:0.78rem; color:#aad4f5; }}
  .btn {{ border:none; border-radius:6px; padding:7px 14px; font-size:0.78rem; font-weight:600; cursor:pointer; }}
  .btn-legend  {{ background:#0f3460; color:#fff; }}
  .btn-print   {{ background:#e94560; color:#fff; }}
  .btn-leg-print {{ background:#1a7a4a; color:#fff; }}
  #map {{ flex:1; }}
  #legend {{ position:fixed; bottom:20px; right:20px; background:rgba(255,255,255,0.97);
    border:1px solid #ccc; border-radius:8px; padding:12px; max-height:70vh; overflow-y:auto;
    min-width:195px; z-index:1000; box-shadow:0 2px 8px rgba(0,0,0,0.15); }}
  #legend h3 {{ font-size:0.72rem; color:#1F4E79; text-transform:uppercase; letter-spacing:1px;
    margin-bottom:8px; border-bottom:1px solid #ddd; padding-bottom:5px; }}
  .leg-item {{ display:flex; align-items:center; gap:7px; margin:3px 0; cursor:pointer;
    padding:2px 4px; border-radius:3px; transition:background 0.15s; }}
  .leg-item:hover {{ background:rgba(0,0,0,0.05); }}
  .leg-dot {{ width:11px; height:11px; border-radius:50%; flex-shrink:0; border:1.5px solid rgba(0,0,0,0.15); }}
  .leg-label {{ font-size:0.7rem; color:#444; }}
  .leg-count {{ font-size:0.65rem; color:#999; margin-left:auto; }}
  #info {{ position:fixed; top:65px; left:10px; background:rgba(255,255,255,0.97);
    border:1px solid #ccc; border-radius:8px; padding:10px; max-width:240px; z-index:1000; display:none;
    box-shadow:0 2px 8px rgba(0,0,0,0.15); }}
  #info h4 {{ font-size:0.82rem; color:#1F4E79; margin-bottom:4px; }}
  #info p {{ font-size:0.72rem; color:#666; line-height:1.5; }}
  #search {{ position:fixed; top:65px; right:215px; z-index:1000; }}
  #search input {{ background:#fff; border:1px solid #ccc; border-radius:6px;
    padding:7px 12px; color:#333; font-size:0.78rem; width:200px; outline:none;
    box-shadow:0 1px 4px rgba(0,0,0,0.1); }}
  #stats {{ position:fixed; top:65px; left:10px; background:rgba(255,255,255,0.95);
    border:1px solid #ccc; border-radius:6px; padding:8px 12px; z-index:1000; font-size:0.72rem; color:#666; }}
  #stats strong {{ color:#1F4E79; font-size:0.95rem; }}
  .tooltip-sec {{ background:rgba(255,255,255,0.9); border:none; border-radius:4px;
    font-size:11px; font-weight:600; color:#333; padding:3px 7px; box-shadow:0 1px 4px rgba(0,0,0,0.2); }}
  @media print {{
    @page {{ margin:0; size:A4 landscape; }}
    header, #search, #stats, #info,
    .leaflet-control-zoom, .leaflet-control-attribution {{ display:none !important; }}
    #legend {{ display:block !important; position:fixed !important; right:10px !important;
      top:10px !important; max-height:90vh !important; overflow-y:auto !important;
      font-size:10px !important; z-index:9999 !important; }}
    * {{ -webkit-print-color-adjust:exact !important; print-color-adjust:exact !important; }}
    body {{ margin:0; padding:0; background:#fff !important; }}
    #map {{ position:fixed !important; top:0; left:0; width:100vw; height:100vh; }}
  }}
</style>
</head>
<body>
<header>
  <h1>🗺 BAI 38 — Carte des Secteurs de Collecte {annee}</h1>
  <span id="total-label">{nb_mag} magasins · {nb_sec} secteurs</span>
  <button class="btn btn-legend" id="btn-legend" onclick="toggleLegende()">☰ Légende</button>
  <button class="btn btn-leg-print" onclick="imprimerLegende()">🖨 Imprimer la légende</button>
  <button class="btn btn-print" onclick="imprimerVue()">🖨 Imprimer la vue</button>
</header>

<div id="search"><input type="text" id="searchbox" placeholder="🔍 Rechercher un magasin..." oninput="filterSearch(this.value)"/></div>
<div id="stats"><strong id="stat-n">{nb_mag}</strong> magasins actifs</div>
<div id="map"></div>
<div id="legend"><h3>Secteurs</h3><div id="leg-content"></div></div>
<div id="info"><h4 id="info-nom"></h4><p id="info-detail"></p></div>

<script>
const MAGASINS = {magasins_json};
const POLYGONES = {polygones_json};

const PALETTE = [
  '#e94560','#f39c12','#2ecc71','#3498db','#9b59b6','#1abc9c',
  '#e67e22','#e91e63','#00bcd4','#8bc34a','#ff5722','#607d8b',
  '#795548','#ff9800','#4caf50','#2196f3','#9c27b0','#f44336',
  '#009688','#cddc39','#ff6f00','#0288d1','#43a047','#7b1fa2',
  '#c62828','#00838f','#558b2f','#4527a0','#ad1457','#37474f',
  '#6d4c41','#ffd600','#00897b','#1565c0','#6a1b9a','#283593',
  '#bf360c','#827717','#33691e','#880e4f','#004d40','#01579b',
  '#e65100','#f57f17','#1b5e20','#311b92','#b71c1c','#0d47a1',
  '#4a148c','#006064','#1a237e','#3e2723','#212121','#37474f',
  '#ff8f00','#558b2f','#00695c','#283593','#4a148c','#bf360c'
];

const secteurs = [...new Set(MAGASINS.map(m => m.Secteur))].sort();
const secColor = {{}};
secteurs.forEach((s, i) => secColor[s] = PALETTE[i % PALETTE.length]);

// Légende
const legDiv = document.getElementById('leg-content');
secteurs.forEach(s => {{
  const count = MAGASINS.filter(m => m.Secteur === s).length;
  const item = document.createElement('div');
  item.className = 'leg-item';
  item.innerHTML = `<div class="leg-dot" style="background:${{secColor[s]}}"></div>
    <span class="leg-label">${{s}}</span><span class="leg-count">${{count}}</span>`;
  item.onclick = () => {{ document.getElementById('searchbox').value = s; filterSearch(s); }};
  legDiv.appendChild(item);
}});

// Carte
const map = L.map('map', {{ center:[45.22,5.72], zoom:11 }});
L.tileLayer('https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
  attribution:'© OpenStreetMap © CARTO', maxZoom:19
}}).addTo(map);

// BAI
L.circleMarker([45.18867,5.68456], {{radius:10,color:'#e94560',fillColor:'#e94560',fillOpacity:1,weight:3}})
  .addTo(map).bindTooltip('🏭 BAI — {adresse_siege}', {{permanent:false}});

// Polygones
const polygonLayer = L.layerGroup().addTo(map);
Object.entries(POLYGONES).forEach(([sec, pts]) => {{
  const color = secColor[sec] || '#888';
  L.polygon(pts.map(p => [p[0],p[1]]), {{
    color, weight:2, opacity:0.8,
    fillColor:color, fillOpacity:0.10,
    dashArray:'5,4', interactive:false
  }}).addTo(polygonLayer);
}});

// Labels secteurs
secteurs.forEach(s => {{
  const pts = MAGASINS.filter(m => m.Secteur === s);
  if (!pts.length) return;
  const lat = pts.reduce((a,b) => a+b.Latitude, 0)/pts.length;
  const lon = pts.reduce((a,b) => a+b.Longitude, 0)/pts.length;
  L.marker([lat,lon], {{
    icon: L.divIcon({{
      html: `<div style="background:${{secColor[s]}}22;border:1px solid ${{secColor[s]}};border-radius:4px;
        padding:2px 5px;font-size:10px;color:${{secColor[s]}};font-weight:600;white-space:nowrap">${{s}}</div>`,
      className:'', iconAnchor:[0,0]
    }})
  }}).addTo(map);
}});

// Marqueurs
const markers = [];
const markerLayer = L.layerGroup().addTo(map);
MAGASINS.forEach(m => {{
  const color = secColor[m.Secteur] || '#888';
  const mk = L.circleMarker([m.Latitude,m.Longitude], {{
    radius:7, color:'#fff', weight:1.5, fillColor:color, fillOpacity:0.9
  }});
  mk.data = m;
  mk.on('click', () => {{
    document.getElementById('info-nom').textContent = m.Nom;
    document.getElementById('info-detail').innerHTML =
      `<span style="color:${{color}}">● ${{m.Secteur}}</span><br>
       <span style="color:#999">${{m.Latitude.toFixed(5)}}, ${{m.Longitude.toFixed(5)}}</span>`;
    document.getElementById('info').style.display = 'block';
  }});
  mk.on('mouseover', function() {{ this.setRadius(10); }});
  mk.on('mouseout',  function() {{ this.setRadius(7); }});
  mk.bindTooltip(`<b>${{m.Nom}}</b><br><span style="color:${{color}}">● ${{m.Secteur}}</span>`, {{direction:'top',offset:[0,-5]}});
  mk.addTo(markerLayer);
  markers.push(mk);
}});

function filterSearch(q) {{
  q = q.toLowerCase().trim();
  markerLayer.clearLayers();
  let count = 0;
  markers.forEach(mk => {{
    const match = !q || mk.data.Nom.toLowerCase().includes(q) || mk.data.Secteur.toLowerCase().includes(q);
    if (match) {{ mk.addTo(markerLayer); count++; }}
  }});
  document.getElementById('stat-n').textContent = count;
}}

function toggleLegende() {{
  const leg = document.getElementById('legend');
  const btn = document.getElementById('btn-legend');
  if (leg.style.display === 'none') {{ leg.style.display='block'; btn.style.background='#0f3460'; }}
  else {{ leg.style.display='none'; btn.style.background='#555'; }}
}}

function imprimerVue() {{
  const ids = ['search','stats','info'];
  const header = document.querySelector('header');
  ids.forEach(id => {{ const el=document.getElementById(id); if(el) el.style.display='none'; }});
  if(header) header.style.display='none';
  document.getElementById('legend').style.display='block';
  document.getElementById('map').style.cssText='position:fixed;top:0;left:0;width:100vw;height:100vh;';
  map.invalidateSize();
  setTimeout(() => {{
    window.print();
    setTimeout(() => {{
      ids.forEach(id => {{ const el=document.getElementById(id); if(el) el.style.display=''; }});
      if(header) header.style.display='';
      document.getElementById('map').style.cssText='';
      map.invalidateSize();
    }}, 1500);
  }}, 500);
}}

function imprimerLegende() {{
  const contenu = document.getElementById('legend').innerHTML;
  const fen = window.open('','_blank','width=400,height=700');
  fen.document.write(`<html><head><title>Légende BAI 38</title>
    <style>
      * {{ -webkit-print-color-adjust:exact !important; print-color-adjust:exact !important; }}
      body {{ font-family:Segoe UI,sans-serif; padding:20px; }}
      h2 {{ color:#1F4E79; font-size:14px; margin-bottom:12px; border-bottom:2px solid #1F4E79; padding-bottom:6px; }}
      .leg-item {{ display:flex; align-items:center; gap:8px; margin:5px 0; }}
      .leg-dot {{ width:12px; height:12px; border-radius:50%; flex-shrink:0; border:1px solid rgba(0,0,0,0.2); }}
      .leg-label {{ font-size:12px; color:#333; }}
      .leg-count {{ font-size:11px; color:#888; margin-left:auto; }}
      @media print {{ @page {{ margin:15mm; }} }}
    </style></head><body>
    <h2>BAI 38 — Secteurs de Collecte {annee}</h2>
    ${{contenu}}
    <script>window.onload=function(){{window.print();window.close();}}<\\/script>
    </body></html>`);
  fen.document.close();
}}

document.getElementById('map').addEventListener('click', () => {{
  document.getElementById('info').style.display = 'none';
}});
</script>
</body>
</html>"""

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)
