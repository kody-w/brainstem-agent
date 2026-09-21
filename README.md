# RAPP Brainstem

The public landing site for **RAPP Brainstem**, a local-first AI agent engine.

**Site:** https://kody-w.github.io/brainstem-agent/

**Runtime and installation documentation:** https://github.com/kody-w/rapp-installer

This repository contains only the static website. It does not contain, modify, or redistribute the Brainstem runtime or installers. The installation controls link to existing upstream production scripts.

## Develop

The website is plain HTML, CSS, and JavaScript. No production build or backend is needed.

```sh
npm ci
npx playwright install chromium
npm test
```

Use Node.js 22 or newer. `npm run serve` starts a preview at `http://127.0.0.1:4173/brainstem-agent/`. The test server uses the same project prefix to match GitHub Pages; set `PORT` to use a different port.

Local tests can reuse installed Chrome or Edge; CI always uses bundled Chromium. Set `PLAYWRIGHT_EXECUTABLE_PATH` to explicitly choose a local browser. See `playwright.config.js` for details.

Production assets are local. There are no analytics, account forms, remote fonts, automatic installer execution, or requests to a visitor's local Brainstem. The walkthrough is explicitly sample data, not a running agent.

## Publish

GitHub Pages uses the Actions workflow in `.github/workflows/pages.yml`. Enable **Settings → Pages → Source → GitHub Actions**. Pushes to `main` run the checks and publish only `index.html` and `assets/`. Manual workflow dispatch is also supported.

Verify the live project URL after deployment; a successful push alone does not mean the site is live.

## Content maintenance

Before changing installation or feature copy:

1. Verify the upstream README, actual installer scripts, and runtime version file. Do not equate GitHub's latest ecosystem release with the Brainstem runtime version.
2. Check that the referenced scripts respond successfully and still match upstream's intended production path. Do not execute installers as part of website tests.
3. Keep account, platform, and remote-inference requirements visible. "Local-first" does not mean inference is offline or that no data leaves the device.
4. Check documentation destinations. Do not assume upstream files are published at equivalent Pages-relative paths.
5. Run the browser suite and inspect desktop/mobile layouts, clipboard failure behavior, no-JavaScript fallback, and keyboard navigation.

The default installers track upstream `main`, not an immutable release. This site intentionally has no runtime-version badge.

## Visual assets

The neural illustration, diagrams, favicon, and social card were created for this site. The design uses broad editorial inspiration, not Hermes artwork, logos, proprietary fonts, or page copy. Refer to any asset-specific attribution files for third-party assets if added later.

## Support

For runtime issues, use [the upstream issue tracker](https://github.com/kody-w/rapp-installer/issues). For website defects, use this repository's issues. Neither is a Microsoft support channel.
