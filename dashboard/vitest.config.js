import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/__tests__/setup.js'],
    globals: true,
    coverage: {
      provider: 'v8',
      include: ['src/hooks/**', 'src/components/**', 'src/ui-preview/**/*.{js,jsx}', 'src/Centre.jsx'],
      thresholds: {
        'src/hooks/**': 100,
        'src/components/**': 80,
        'src/ui-preview/**/*.{js,jsx}': { lines: 95, statements: 95, branches: 90, functions: 90 },
      },
    },
  },
});
