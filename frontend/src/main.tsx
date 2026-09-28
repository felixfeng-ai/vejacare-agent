import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import App from './App';
import './styles/globals.css';

const container = document.getElementById('root');
if (container === null) {
  throw new Error('找不到 #root 容器，请检查 index.html');
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
