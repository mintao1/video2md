import js from '@eslint/js'
import prettier from 'eslint-config-prettier'
import vue from 'eslint-plugin-vue'
import globals from 'globals'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  { ignores: ['dist/', 'node_modules/', 'playwright-report/', 'test-results/'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...vue.configs['flat/recommended'],
  {
    files: ['**/*.vue'],
    languageOptions: { parserOptions: { parser: tseslint.parser } },
  },
  {
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
    rules: {
      // Single-word view/component names (App, BookCover…) predate this config.
      'vue/multi-word-component-names': 'off',
      // The only v-html sink (ReaderView) is sanitized by DOMPurify; a new one
      // must carry an explicit eslint-disable with the reason.
      'vue/no-v-html': 'error',
    },
  },
  prettier,
)
