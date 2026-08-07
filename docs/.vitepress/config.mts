import { defineConfig } from 'vitepress'

export default defineConfig({
  title: 'allowances',
  description:
    'Grants and consumable budgets — enforced before the fact, auditable after it. Storage-agnostic, with a Django backend.',
  lastUpdated: true,
  cleanUrls: true,

  themeConfig: {
    nav: [
      { text: 'Guide', link: '/guide/', activeMatch: '/guide/' },
      { text: 'Backends', link: '/backends/', activeMatch: '/backends/' },
      { text: 'API', link: '/api/core', activeMatch: '/api/' },
    ],

    sidebar: {
      '/guide/': [
        {
          text: 'Introduction',
          items: [
            { text: 'What it is', link: '/guide/' },
            { text: 'Getting started', link: '/guide/getting-started' },
            { text: 'Concepts', link: '/guide/concepts' },
          ],
        },
        {
          text: 'Using it',
          items: [
            { text: 'Fulfillment order', link: '/guide/fulfillment-order' },
            { text: 'Sliding windows', link: '/guide/sliding-windows' },
            { text: 'Overdraft', link: '/guide/overdraft' },
            { text: 'Reversals and recovery', link: '/guide/reversals' },
            { text: 'Plans and archival', link: '/guide/plans-and-archival' },
          ],
        },
      ],
      '/backends/': [
        {
          text: 'Backends',
          items: [
            { text: 'How the port works', link: '/backends/' },
            { text: 'Django', link: '/backends/django' },
            { text: 'In-memory', link: '/backends/memory' },
            { text: 'Writing a backend', link: '/backends/writing-a-backend' },
            { text: 'Would Redis work?', link: '/backends/redis' },
          ],
        },
      ],
      '/api/': [
        {
          text: 'API',
          items: [
            { text: 'Core', link: '/api/core' },
            { text: 'Django adapter', link: '/api/django' },
          ],
        },
      ],
    },

    outline: [2, 3],

    search: { provider: 'local' },

    socialLinks: [],

    footer: {
      message: 'MIT licensed.',
    },
  },
})
