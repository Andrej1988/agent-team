import { Navigate, useParams } from 'react-router-dom'
import { Pill, PillRow } from '../components/nav/Pills'
import DockerMetrics from './system/DockerMetrics'
import HostMetrics from './system/HostMetrics'

// System section: host metrics (collector service) and Docker metrics.
export default function System() {
  const { part = '' } = useParams()
  if (part !== 'host' && part !== 'docker') return <Navigate to="/system/host" replace />
  return (
    <div className="space-y-3">
      <PillRow label="System">
        <Pill to="/system/host">Host</Pill>
        <Pill to="/system/docker">Docker</Pill>
      </PillRow>
      {part === 'host' ? <HostMetrics /> : <DockerMetrics />}
    </div>
  )
}
