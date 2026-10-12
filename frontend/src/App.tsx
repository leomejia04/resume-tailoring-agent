import { Route, Routes } from 'react-router'
import { RequireAuth } from './auth/RequireAuth'
import { Layout } from './components/Layout'
import { PublicLayout } from './components/PublicLayout'
import { ForgotPasswordPage } from './pages/auth/ForgotPasswordPage'
import { ResetPasswordPage } from './pages/auth/ResetPasswordPage'
import { SignInPage } from './pages/auth/SignInPage'
import { SignUpPage } from './pages/auth/SignUpPage'
import { DashboardPage } from './pages/DashboardPage'
import { ExperiencePage } from './pages/ExperiencePage'
import { JobInputPage } from './pages/JobInputPage'
import { NotFoundPage } from './pages/NotFoundPage'
import { ProfilePage } from './pages/ProfilePage'
import { RunPage } from './pages/RunPage'
import { SettingsPage } from './pages/SettingsPage'
import { TrackerPage } from './pages/TrackerPage'

export default function App() {
  return (
    <Routes>
      <Route element={<PublicLayout />}>
        <Route path="sign-in" element={<SignInPage />} />
        <Route path="sign-up" element={<SignUpPage />} />
        <Route path="forgot-password" element={<ForgotPasswordPage />} />
        <Route path="reset-password" element={<ResetPasswordPage />} />
      </Route>
      <Route element={<RequireAuth />}>
        <Route element={<Layout />}>
          <Route index element={<DashboardPage />} />
          <Route path="jobs/new" element={<JobInputPage />} />
          <Route path="experience" element={<ExperiencePage />} />
          <Route path="runs/:runId" element={<RunPage />} />
          <Route path="profile" element={<ProfilePage />} />
          <Route path="tracker" element={<TrackerPage />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Route>
    </Routes>
  )
}
