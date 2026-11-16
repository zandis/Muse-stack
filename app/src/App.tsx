import { Shell } from './components/Shell';
import type { AppClient } from './home/app-client';
import type { HomeClient } from './home/client';
import { I18nProvider } from './i18n';
import { RouterProvider, useRouter, type Screen } from './router';
import { Ask } from './screens/Ask';
import { Chores } from './screens/Chores';
import { FirstRun } from './screens/FirstRun';
import { ForApps } from './screens/ForApps';
import { Permissions } from './screens/Permissions';
import { More, Placeholder, Wallet } from './screens/Placeholders';
import { Records } from './screens/Records';
import { Services } from './screens/Services';
import { Settings } from './screens/Settings';
import { Today } from './screens/Today';
import { Tray } from './screens/Tray';
import { AppClientProvider } from './state/app-client';
import { StoreProvider } from './state/store';

function ScreenFor({ screen }: { screen: Screen }) {
  switch (screen) {
    case 'today':
      return <Today />;
    case 'ask':
      return <Ask />;
    case 'chores':
      return <Chores />;
    case 'more':
      return <More />;
    case 'services':
      return <Services />;
    case 'wallet':
      return <Wallet />;
    case 'settings':
      return <Settings />;
    case 'apps':
      return <ForApps />;
    case 'records':
      return <Records />;
    case 'permissions':
      return <Permissions />;
    default:
      return <Placeholder screen={screen} />;
  }
}

function Routes() {
  const { route } = useRouter();
  if (route === 'tray') return <Tray />;
  if (route === 'welcome') return <FirstRun />;
  return (
    <Shell screen={route}>
      <ScreenFor screen={route} />
    </Shell>
  );
}

export function App({ client, appClient, now }: { client: HomeClient; appClient: AppClient; now?: () => number }) {
  return (
    <I18nProvider>
      <StoreProvider client={client} {...(now !== undefined ? { now } : {})}>
        <AppClientProvider client={appClient}>
          <RouterProvider>
            <Routes />
          </RouterProvider>
        </AppClientProvider>
      </StoreProvider>
    </I18nProvider>
  );
}
