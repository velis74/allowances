import velis from 'eslint-config-velis';

export default [
  ...velis,
  {
    ignores: ['.venv/*', 'node_modules/*', 'docs/.vitepress/cache/*', 'docs/.vitepress/dist/*'],
  },
];
