# Analyse comparative des 8 simulations de tournées — Collecte 2026 (Vendredi/Samedi)

*Périmètre : Vendredi et Samedi (Matin + Après-midi), hors véhicules figés. Toutes les valeurs ci-dessous sont reprises telles quelles des sorties de l'algorithme d'optimisation, sauf mention explicite « calculé » (dérivé par simple arithmétique à partir des données fournies).*

## 1. Dépassements de capacité : MAX4 est-il structurellement adapté ?

| | VX1 | VX2 | VX3 | VX4 |
|---|---|---|---|---|
| Surcharges MAX4 | 10 | 7 | 4 | **3** |
| Surcharges MAX5 | 1 | **0** | 0 | 0 |

**MAX4 n'est pas structurellement adapté.** Même en poussant l'investissement au maximum simulé (4 camions VX, 32 magasins délestés), il reste **3 tournées en surcharge** (V003 Vendredi Après-midi, V032 Samedi Matin, V032 Samedi Après-midi). La courbe de décroissance (10 → 7 → 4 → 3) s'aplatit fortement entre VX3 et VX4 (−1 seulement, contre −3 puis −3 sur les paliers précédents) : le 4ᵉ camion touche un rendement décroissant net et ne suffit jamais à ramener les surcharges à zéro. Autrement dit, sous MAX4, il existe un noyau dur de tournées (V003, V032) que l'ajout de camions ne résorbe pas — c'est un plafond structurel du réglage MAX4, pas un manque de moyens.

**Avec MAX5, les surcharges disparaissent totalement dès VX2** (2 camions supplémentaires). VX1-MAX5 conserve encore 1 surcharge résiduelle (V016, Vendredi Matin, 6 magasins — c'est aussi la seule tournée à 6+ magasins de tout le jeu MAX5). À partir de VX2-MAX5, VX3-MAX5 et VX4-MAX5, le taux de surcharge est nul et le reste stable à 0.

**Réponse directe :** MAX4 doit être écarté comme cible — il ne permet jamais d'atteindre zéro surcharge, quel que soit le nombre de camions. MAX5 est le seuil qui rend l'objectif atteignable, et **2 camions VX suffisent** pour l'atteindre.

## 2. Rôle de chaque camion supplémentaire (VX)

| Scénario | Camions créés | Détail par créneau (secteur → nb magasins) | Magasins délestés |
|---|---|---|---|
| VX1-MAX4 | 1 | VX300 : VM Grenoble Centre Ouest (2) · AM Sud Vercors (3) · SM Sud Vercors (3) · SAM Sud Vercors (3) | 11 |
| VX2-MAX4 | 2 | VX300 : VM Grenoble Centre Ouest (2) · AM/SM/SAM Sud Vercors (3+3+3) — VX301 : VM Sud Vercors (3) · AM/SM/SAM Gresivaudan (3+3+3) | 23 |
| VX3-MAX4 | 3 | idem VX2 + VX302 : VM Gresivaudan (3) · AM Voiron\|Moirans (3) — **actif uniquement le vendredi** | 29 |
| VX4-MAX4 | 4 | idem VX3 + VX303 : VM Voiron\|Moirans (3) — **actif sur 1 seul créneau (Vendredi Matin)** | 32 |
| VX1-MAX5 | 1 | VX300 : VM Saint Martin d'Hères (4) · AM/SM/SAM Sud Vercors (3+3+3) | 13 |
| VX2-MAX5 | 2 | VX300 (idem VX1) + VX301 : VM Sud Vercors (3) — **actif sur 1 seul créneau** | 16 |
| VX3-MAX5 | 3 | idem VX2 + VX302 : VM Gresivaudan (4) — **actif sur 1 seul créneau** | 20 |
| VX4-MAX5 | **3** (pas 4) | strictement identique à VX3-MAX5 (mêmes VX300/301/302, mêmes secteurs, mêmes volumes) | 20 |

Trois constats factuels :

- **Le secteur Sud Vercors est systématiquement le premier à être délesté** (VX300, présent dans les 8 scénarios), et reste porté par un seul camion sur les 3 créneaux Après-midi/Samedi dans presque tous les cas.
- **Les camions ajoutés au-delà du 2ᵉ sont sous-utilisés** : VX302 (MAX4 et MAX5) et VX303 (MAX4) n'opèrent que sur un ou deux créneaux du vendredi, jamais sur les 4 créneaux — signe que le besoin de délestage est concentré sur le vendredi matin/après-midi, pas sur le samedi.
- **Sous MAX5, le 4ᵉ camion autorisé n'est jamais créé** : « Camions VX créés » reste à 3 pour VX4-MAX5, avec une configuration de camions strictement identique à VX3-MAX5. Autoriser un 4ᵉ camion sous MAX5 est donc sans effet sur le dimensionnement de la flotte — l'algorithme n'en a pas besoin.

## 3. Cohérence sectorielle : contrainte géographique ou réglage perfectible ?

Les tournées à 3 secteurs ou plus (aucune tournée n'atteint 4+ secteurs dans les 8 scénarios) permettent d'isoler ce qui relève de la géographie plutôt que du paramétrage :

- **V025** (Sassenage \| La Tronche \| Saint Martin d'Hères / Grenoble Nord / Seyssinet / Saint Martin le Vinoux, selon le créneau) et **V001** (Échirolles \| Saint Martin d'Hères \| Bresson) apparaissent sur **tous les niveaux de VX en MAX4** (VX1 à VX4), sur 4 créneaux distincts chacun — persistance totale sous MAX4. **Mais ces deux tournées disparaissent complètement de la liste dès qu'on passe en MAX5.** Cela indique que leur caractère multi-secteurs sous MAX4 est en grande partie un **artefact du plafond à 4 magasins** : la 5ᵉ place disponible en MAX5 permet de regrouper ces arrêts dans des tournées mono- ou bi-secteur.
- **V018** (Rives \| Moirans \| Voiron) et **V031** (Grenoble Centre Est/Nord \| Seyssinet \| La Tronche \| Grenoble Nord, selon le créneau) apparaissent en revanche **sous MAX4 ET sous MAX5**, quel que soit le nombre de camions. En particulier, **V031 (créneau Après-midi, variante MAX5 à 5 magasins) est présent dans les 4 scénarios MAX5 sans exception** (VX1 à VX4) : c'est la seule tournée que ni le réglage MAX5 ni l'ajout de camions ne parvient à éliminer. C'est le signe d'une **contrainte géographique structurelle réelle** (stores dispersés sur un axe Rives/Moirans/Voiron/Grenoble Nord qui ne peuvent pas être regroupés autrement).

**Conclusion :** la majorité de l'« incohérence sectorielle » observée sous MAX4 (V025, V001, jusqu'à 11 tournées à 3 secteurs) est corrigible par le passage à MAX5, et n'est donc pas une fatalité géographique. Le résidu incompressible — V018 et surtout V031 — représente la vraie contrainte structurelle du territoire, présente quel que soit le scénario.

## 4. Analyse détaillée par configuration

**VX1-MAX4** — 1 camion, 2367.7 km (le plus faible de la famille MAX4).
*Points positifs* : investissement minimal, kilométrage contenu.
*Points négatifs* : pire résultat des 8 scénarios sur les surcharges (10, dont 1 tournée à 6 magasins), 11 tournées à 3 secteurs — configuration à écarter en l'état.

**VX2-MAX4** — 2 camions, 2492.2 km.
*Points positifs* : surcharges réduites à 7 (−3 vs VX1).
*Points négatifs* : encore 7 tournées en surcharge et 9 à 3 secteurs ; +124.5 km pour un résultat qui reste loin de zéro.

**VX3-MAX4** — 3 camions, 2563.5 km.
*Points positifs* : meilleur compromis de la famille MAX4 (4 surcharges, 8 tournées à 3 secteurs).
*Points négatifs* : +195.8 km vs VX1-MAX4, sans jamais atteindre zéro surcharge.

**VX4-MAX4** — 4 camions, 2604.9 km (le kilométrage le plus élevé des 8 scénarios).
*Points positifs* : le plus grand nombre de tournées gérées (100) et le meilleur score sectoriel de la famille MAX4 (7).
*Points négatifs* : le 4ᵉ camion (VX303) n'opère qu'un seul créneau sur quatre ; malgré cela, 3 tournées restent en surcharge — rendement décroissant flagrant, plafond structurel atteint.

**VX1-MAX5** — 1 camion, 2321.4 km (le plus bas des 8 scénarios), temps total estimé (calculé : nb tournées × durée moyenne) ≈ 15 298,5 min, le plus bas des 8.
*Points positifs* : quasi-suppression des surcharges (1 seule, contre 10 en VX1-MAX4) avec un seul camion.
*Points négatifs* : la tournée résiduelle (V016, 6 magasins) reste un point de vigilance opérationnelle.

**VX2-MAX5** — 2 camions, 2340.1 km.
*Points positifs* : **zéro surcharge**, kilométrage très proche du minimum (+18.7 km vs VX1-MAX5), temps total estimé ≈ 15 322 min.
*Points négatifs* : aucun identifié à ce niveau — c'est le seuil où l'objectif « zéro surcharge » est atteint au moindre coût.

**VX3-MAX5** — 3 camions, 2372.9 km.
*Points positifs* : zéro surcharge maintenu.
*Points négatifs* : le 3ᵉ camion (VX302) n'opère qu'un seul créneau sur quatre ; +32.8 km vs VX2-MAX5 sans gain mesurable sur les surcharges (déjà à 0) ni sur les tournées à 3 secteurs (stable à 3).

**VX4-MAX5** — 3 camions réellement créés (4 autorisés), 2397.1 km.
*Points positifs* : meilleur score sectoriel de tout le jeu de données (2 tournées à 3 secteurs, contre 3 partout ailleurs en MAX5).
*Points négatifs* : ce gain d'une seule tournée coûte +24.2 km vs VX3-MAX5, et le 4ᵉ camion autorisé n'est jamais mobilisé — la configuration ne se distingue de VX3-MAX5 que par un réarrangement marginal, pas par un moyen supplémentaire réellement engagé.

## 5. Recommandation finale

| Scénario | Surcharges | Km total | Tournées 3+ secteurs | Camions VX réellement créés | Verdict |
|---|---|---|---|---|---|
| VX1-MAX4 | 10 | 2367.7 | 11 | 1 | ✗ |
| VX2-MAX4 | 7 | 2492.2 | 9 | 2 | ✗ |
| VX3-MAX4 | 4 | 2563.5 | 8 | 3 | ~ |
| VX4-MAX4 | 3 | 2604.9 | 7 | 4 | ✗ |
| VX1-MAX5 | 1 | 2321.4 | 3 | 1 | ✓ |
| **VX2-MAX5** | **0** | **2340.1** | 3 | **2** | **★** |
| VX3-MAX5 | 0 | 2372.9 | 3 | 3 | ~ |
| VX4-MAX5 | 0 | 2397.1 | 2 | 3 (4 autorisés, non utilisé) | ~ |

**★ VX2-MAX5 — configuration recommandée.** C'est le point où l'objectif opérationnel (zéro surcharge, donc zéro tournée matériellement infaisable) est atteint avec le moins de moyens : seulement 2 camions supplémentaires, un kilométrage à seulement +18.7 km du minimum absolu du jeu de données, et un temps total de tournées (calculé) parmi les plus bas. Aller au-delà (VX3 ou VX4) n'apporte aucun bénéfice sur les surcharges (déjà nulles) et coûte des kilomètres supplémentaires pour des camions sous-utilisés.

**✓ VX1-MAX5 — bonne alternative si la flotte est très contrainte.** Kilométrage et temps total les plus bas des 8 scénarios, un seul camion nécessaire. À condition d'accepter un suivi manuel de la tournée V016 (6 magasins, Vendredi Matin), seul point de vigilance résiduel.

**~ VX3-MAX4 — acceptable avec nuances uniquement si MAX5 est inapplicable opérationnellement** (contrainte véhicule réelle limitant à 4 arrêts). C'est le meilleur compromis de la famille MAX4, mais il ne doit pas être confondu avec une solution : 4 tournées restent en surcharge de façon irréductible.

**~ VX3-MAX5 et VX4-MAX5 — acceptables mais sans valeur ajoutée démontrée** par rapport à VX2-MAX5. Le 3ᵉ camion n'opère qu'un seul créneau sur quatre, et le 4ᵉ camion autorisé sous VX4-MAX5 n'est jamais créé par l'algorithme. Le seul gain (VX4-MAX5 : une tournée à 3 secteurs de moins) se paie en kilomètres sans réduction des surcharges, déjà nulles.

**✗ VX1-MAX4, VX2-MAX4, VX4-MAX4 — à éviter.** Ces trois configurations ne descendent jamais à zéro surcharge sous MAX4, y compris en poussant l'investissement à 4 camions ; leur kilométrage est par ailleurs égal ou supérieur aux meilleures configurations MAX5.

**En synthèse : abandonner MAX4 comme cible pour la collecte 2026** — le plafond à 4 magasins par tournée constitue une limite structurelle qui ne se résout pas par l'ajout de camions. **Retenir MAX5 avec 2 camions supplémentaires (VX2-MAX5)** comme configuration standard, et garder VX1-MAX5 en solution de repli si la flotte disponible est limitée à 1 camion.
