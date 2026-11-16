import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { defaultAppClient, defaultClient } from './home';
import './styles.css';
import './screens.css';

const root = document.getElementById('root');
if (root) {
  createRoot(root).render(
    <StrictMode>
      <App client={defaultClient()} appClient={defaultAppClient()} />
    </StrictMode>,
  );
}
