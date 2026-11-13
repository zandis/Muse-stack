import { Shell } from './components/Shell';
import type { HomeClient } from './home/client';
import { I18nProvider } from './i18n';
import { RouterProvider, useRouter, type Screen } from './router';
import { More, Placeholder, Services, Settings, Wallet } from './screens/Placeholders';
import { Today } from './screens/Today';
import { Tray } from './screens/Tray';
import { StoreProvider } from './state/store';

function ScreenFor({ screen }: { screen: Screen }) {
  switch (screen) {
    case 'today':
      return <Today />;
    case 'more':
      return <More />;
    case 'services':
      return <Services />;
    case 'wallet':
      return <Wallet />;
    case 'settings':
      return <Settings />;
    default:
      return <Placeholder screen={screen} />;
  }
}

function Routes() {
  const { route } = useRouter();
  if (route === 'tray') return <Tray />;
  return (
    <Shell screen={route}>
      <ScreenFor screen={route} />
    </Shell>
  );
}

export function App({ client }: { client: HomeClient }) {
  return (
    <I18nProvider>
      <StoreProvider client={client}>
        <RouterProvider>
          <Routes />
        </RouterProvider>
      </StoreProvider>
    </I18nProvider>
  );
}
