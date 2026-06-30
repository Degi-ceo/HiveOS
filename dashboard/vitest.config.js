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
      include: ['src/hooks/**', 'src/components/**', 'src/Centre.jsx'],
      thresholds: { 'src/hooks/**': 100, 'src/components/**': 80 },
    },
  },
});