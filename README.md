# Radar Reflex — veille des appels d'offres WMS

Deux briques indépendantes :

1. **`index.html`** — le site consultable. Interroge l'API BOAMP directement
   depuis le navigateur, filtre et note les avis, les affiche par pertinence.
2. **`boamp_scan.py`** — le script d'alerte email. Fait la même chose côté
   serveur et envoie un digest par email pour les nouveaux avis uniquement.

Les deux utilisent la même logique de score (1 à 5) :
- **5/5** : le nom "Reflex" ou "Hardis" apparaît explicitement dans l'avis.
- **3-4/5** : contexte WMS générique (gestion d'entrepôt, warehouse management…).
- **1-2/5** : contexte logistique large, à vérifier manuellement.

## ⚠️ À vérifier avant la mise en production

Je n'ai pas pu tester les appels réels à l'API depuis cet environnement
(pas d'accès réseau sortant ici). Le nom des champs utilisés dans le code
(`objet`, `nomacheteur`, `code_departement`, `dateparution`, `url_avis`)
est basé sur la documentation et des réutilisations publiques du jeu de
données, mais **le schéma BOAMP a changé plusieurs fois** (passage aux
eForms européens en 2024 notamment). Avant de déployer :

1. Ouvrez la console de l'API : https://boamp-datadila.opendatasoft.com/explore/dataset/boamp/api/
2. Lancez une requête de test et comparez les noms de champs réels à ceux du code.
3. Ajustez `fieldGuess()` dans `index.html` et `parse_record()` dans
   `boamp_scan.py` si nécessaire — c'est la seule partie susceptible de
   nécessiter un ajustement.

## Déployer le site (`index.html`)

Le site est un fichier unique, sans backend. Le plus simple :

- **Netlify / Vercel** : glissez-déposez le fichier `index.html` sur
  app.netlify.com/drop (aucun compte requis pour un premier essai), ou
  connectez un dépôt GitHub pour un déploiement automatique à chaque
  modification.
- **GitHub Pages** : poussez ce dossier dans un dépôt GitHub, activez
  Pages dans les paramètres du dépôt (branche `main`, dossier racine).

Point d'attention : l'appel à l'API BOAMP se fait depuis le navigateur de
l'utilisateur (CORS). Si l'API bloque les appels cross-origin, le site
affichera un message d'erreur explicite — dans ce cas, il faudra passer
par un petit proxy (une fonction serverless Netlify/Vercel suffit) qui
relaie la requête. Dites-le-moi si ce cas se présente, je l'ajouterai.

## Mettre en place les alertes email (`boamp_scan.py`)

Recommandation : **GitHub Actions**, gratuit pour ce volume d'usage et
déjà configuré dans `.github/workflows/scan.yml` (exécution quotidienne
à 7h locales, plus déclenchement manuel possible).

Étapes :

1. Créez un dépôt GitHub et poussez-y tout ce dossier.
2. Dans **Settings → Secrets and variables → Actions**, ajoutez ces secrets :
   - `SMTP_HOST` (ex. `smtp.gmail.com`)
   - `SMTP_PORT` (ex. `587`)
   - `SMTP_USER` (votre adresse d'envoi)
   - `SMTP_PASSWORD` (un mot de passe d'application, pas votre mot de passe principal)
   - `ALERT_EMAIL_FROM`
   - `ALERT_EMAIL_TO` (une ou plusieurs adresses séparées par des virgules)
3. Le workflow tourne automatiquement chaque jour. Vous pouvez aussi le
   lancer à la main depuis l'onglet **Actions** du dépôt (`Run workflow`).
4. Le fichier `seen_ids.json` (créé automatiquement) garde la trace des
   avis déjà signalés, pour ne jamais recevoir deux fois la même alerte.

### Fournisseur SMTP

Gmail fonctionne pour démarrer (avec un mot de passe d'application, pas
le mot de passe du compte), mais a des quotas d'envoi limités. Pour un
usage plus sérieux, un service comme Resend, Brevo (ex-Sendinblue) ou
Amazon SES offre un tier gratuit largement suffisant à ce volume
(quelques emails par jour) et une meilleure délivrabilité.

## Élargir la couverture au-delà du BOAMP

Les profils d'acheteurs (PLACE, AWS-Achat, Maximilien…) ne sont pas tous
couverts par une API unifiée — certains n'ont qu'un flux RSS, d'autres
rien du tout. Si vous voulez les ajouter, la meilleure approche est de
traiter chaque plateforme au cas par cas plutôt que de chercher un
agrégateur universel. Je peux vous aider à en ajouter une à la fois.
