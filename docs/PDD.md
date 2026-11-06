# PDD — Traitement automatisé des factures fournisseurs

*Process Definition Document. Le processus et le client (« Société Cliente Démo SAS ») sont fictifs : ils
servent de cadre au laboratoire. Les volumes ci-dessous sont des hypothèses de travail, pas des mesures.*

## 1. Contexte et objectif

Le service comptabilité fournisseurs reçoit les factures papier ou PDF, numérisées dans un dossier partagé.
Aujourd'hui, un gestionnaire ouvre chaque facture, saisit les champs dans l'ERP, contrôle le fournisseur et
le bon de commande, puis comptabilise.

Objectif : le robot lit et contrôle chaque facture. Il comptabilise seul celles qui sont conformes et transmet
les autres au métier avec un motif explicite. **Règle d'or : une facture non conforme ne doit jamais être
comptabilisée.** Mieux vaut une facture de plus en contrôle manuel qu'un paiement à tort.

## 2. AS-IS (manuel)

1. Récupérer la facture dans `\\partage\factures\entrantes`.
2. Lire SIRET, n° de facture, date, n° de commande, HT, TVA, TTC.
3. Vérifier que le fournisseur existe dans le référentiel.
4. Vérifier que la facture n'a pas déjà été saisie.
5. Rapprocher avec le bon de commande (existence, fournisseur, montant HT).
6. Saisir et comptabiliser dans l'ERP.
7. En cas d'anomalie : mail à l'acheteur ou au fournisseur, suivi dans un fichier Excel.

## 3. TO-BE (automatisé)

| Étape | Acteur | Détail |
|---|---|---|
| Dépôt des scans | Métier / scanner | Inchangé : dossier partagé |
| Mise en file | Robot *Dispatcher* | 1 élément de file par fichier, référence = empreinte SHA-256 du fichier |
| Lecture | Robot *Performer* | OCR + extraction des 7 champs, confiance par champ |
| Contrôles | Robot *Performer* | Règles RG-01 à RG-07 ci-dessous |
| Comptabilisation | Robot *Performer* | API ERP, clé d'idempotence = référence de l'élément |
| Exceptions métier | Métier | Rapport Excel quotidien « À traiter », motif et action attendue |
| Exceptions système | Support RPA N2 | [Runbook](runbook-support-n2.md) |

## 4. Règles de gestion

| Id | Règle | Si non respectée |
|---|---|---|
| RG-01 | Les 7 champs obligatoires sont lus et le SIRET passe le contrôle de Luhn | `BE_EXTRACTION_REVIEW` — vérification humaine |
| RG-02 | N° de facture et date lus avec une confiance OCR ≥ 50 | `BE_EXTRACTION_REVIEW` |
| RG-03 | HT + TVA = TTC et TVA = 20 % du HT (à 1 centime près) | `BE_TOTAL_MISMATCH`, ou `BE_EXTRACTION_REVIEW` si un des montants est lu avec une confiance < 50 (on ne peut pas savoir si l'erreur vient du fournisseur ou de la lecture) |
| RG-04 | Le SIRET existe dans le référentiel fournisseurs | `BE_UNKNOWN_SUPPLIER` |
| RG-05 | Couple (SIRET, n° de facture) jamais comptabilisé | `BE_DUPLICATE_INVOICE` |
| RG-06 | Le bon de commande existe et appartient à ce fournisseur | `BE_PO_NOT_FOUND` |
| RG-07 | Écart HT facture / HT commande ≤ 1 % | `BE_PO_AMOUNT_MISMATCH` |

Les règles sont appliquées dans cet ordre. La première règle violée donne le motif.

## 5. Exceptions

| Type | Exemples | Traitement |
|---|---|---|
| **Métier** (*Business Exception*) | toutes les `BE_*` ci-dessus | Pas de relance. L'élément passe en *Failed* et part dans le rapport Excel du métier |
| **Système** (*Application Exception*) | ERP en 503, délai dépassé, réponse perdue, fichier introuvable | Relance automatique par l'Orchestrator (2 relances maximum). Au-delà, intervention du support N2 |

## 6. Étude de faisabilité (grille utilisée)

| Critère | Évaluation | Commentaire |
|---|---|---|
| Règles explicites et stables | Favorable | 7 règles écrites, sans jugement humain |
| Entrées structurées | Moyen | Documents semi-structurés : OCR et extraction nécessaires, mises en page variables |
| Volume et répétitivité | À confirmer | À mesurer sur 3 mois d'historique avant engagement |
| Accès aux systèmes | Favorable | API ERP disponible : pas d'automatisation d'interface, donc plus robuste |
| Risque en cas d'erreur | Élevé | Paiement à tort : d'où la règle d'or et les contrôles croisés |
| Exceptions | Maîtrisables | Motifs finis, chacun avec un responsable identifié |

Conclusion : faisable, à condition de rendre l'extraction robuste aux nouvelles mises en page et de garder
l'humain sur tout ce qui n'est pas certain. C'est ce que mesure le
[README](../README.md#results). La mise en page D, inconnue des règles, simule l'arrivée d'un nouveau
fournisseur.
