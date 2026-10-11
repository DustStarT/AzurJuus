Global shell and cross-feature responsive rules live here. Feature styles stay beside their components.

Shared JUUS colors, dimensions, radii and durations start in `../theme.css`.
`blue-glass.css` is the final material layer: navy navigation, translucent blue-white
surfaces, restrained shadows, and accent states shared by chat, moments, settings,
and the work drawer. Keep new feature layouts in their feature stylesheet and use
the shared tokens instead of adding another page-wide palette override.
Responsive rules change layout and inherited shell variables, not message density.
Feature components should use these tokens instead of introducing independent themes.
Motion is limited to entry and interaction feedback; the existing reduced-motion
setting and operating-system preference disable it. Reduced-transparency preference
uses opaque surfaces.

The desktop shell sets `data-desktop-renderer` on the root document after each
navigation. Software rendering uses the same blue palette and interaction motion,
but replaces live background filters with translucent gradients and reduces bubble
shadow blur. Keep this scoped to software mode; browser and automatic Qt rendering
retain the full glass material. Avoid adding unbounded blur filters to scrolling
messages or cards without measuring their software-rendering cost.
