import React, { useEffect, useState } from 'react';
import { useGateway } from '../hooks/useGateway';

export function SkillLauncher({ token, onLaunch }) {
  const { get } = useGateway(token);
  const [skills, setSkills] = useState([]);

  useEffect(() => {
    let mounted = true;
    get('/skills?pinned=true')
      .then((d) => { if (mounted) setSkills(d.pinned || []); })
      .catch(() => { if (mounted) setSkills([]); });
    return () => { mounted = false; };
  }, [get]);

  return (
    <div className="glass skill-launcher" data-testid="skill-launcher">
      <h3>skills</h3>
      {skills.length === 0 && (
        <div className="text-dim" data-testid="skill-empty">no pinned skills</div>
      )}
      <div className="skill-grid">
        {skills.map((s) => (
          <button
            key={s}
            type="button"
            className="skill-chip"
            data-testid="skill-chip"
            data-skill={s}
            onClick={() => onLaunch?.(s)}
          >
            {s}
          </button>
        ))}
      </div>
    </div>
  );
}

export default SkillLauncher;