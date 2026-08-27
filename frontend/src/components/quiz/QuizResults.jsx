import React, { useContext, useEffect, useRef } from 'react';
import { FileText, Download, BarChart3, Sparkles, TrendingUp, Award } from 'lucide-react';
import { ThemeContext } from '@/context/ThemeContext';

const QuizResults = ({
  results,
  quiz,
  completedLevels,
  isGenerating,
  showReview,
  onToggleReview,
  onDownloadPDF,
  onShowHistory,
  onRestart,
  onNextLevel,
  onRetryLevel,
}) => {
  const { isDark } = useContext(ThemeContext);

  // Safety net: `results` is only expected to be briefly null while a new level is actively being generated
  const bailTimerRef = useRef(null);
  useEffect(() => {
    if (!results && !isGenerating) {
      bailTimerRef.current = setTimeout(() => onRestart?.(), 4000);
    }
    return () => clearTimeout(bailTimerRef.current);
  }, [results, isGenerating, onRestart]);

  if (!results) return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', minHeight: '100vh', gap: '1rem' }}>
      <div className='spinner spinner--lg'></div>
      <p style={{ color: 'var(--color-text-muted)', fontSize: '0.9rem' }}>Generating next quiz…</p>
    </div>
  );

  const percentage  = results.score_percentage ?? results.percentage ?? 0;
  const correct     = results.correct_answers   || 0;
  const total       = results.total_questions   || 0;
  const incorrect   = total - correct;
  const timeTaken   = results.time_taken        || 0;
  const passed      = percentage >= 50;
  const scoreColor  = '#9333ea';
  const radius      = 70;
  const circumference = 2 * Math.PI * radius;
  const dashOffset  = circumference - (percentage / 100) * circumference;

  const getDifficultyColor = (d) =>
    ({ easy: '#10b981', medium: '#f59e0b', hard: '#ef4444' })[d?.toLowerCase()] || '#6b7280';

  const getDifficultyIcon = (d) =>
    ({ easy: '⭐', medium: '⭐⭐', hard: '⭐⭐⭐' })[d?.toLowerCase()] || '⭐';

  const getLevelStepClass = (level) => {
    if (!results) return '';
    const currentDiff = results.current_difficulty?.toLowerCase();
    const nextDiff    = results.next_difficulty?.toLowerCase();
    const levelLower  = level.toLowerCase();
    if (completedLevels.includes(levelLower)) return 'completed';
    if (currentDiff === levelLower) return 'completed';
    if (nextDiff === levelLower)    return 'current';
    return '';
  };

  return (
    <div className="results-container">
      <div className="results-header">
        <h1>Quiz Results</h1>
        <p className="subtitle">Here's how you performed on this quiz</p>
      </div>

      {/* Donut Score */}
      <div className="score-card-new">
        <svg className="score-donut" viewBox="0 0 180 180" width="180" height="180">
          <circle cx="90" cy="90" r={radius} fill="none" stroke="#e5e7eb" strokeWidth="14" />
          <circle
            cx="90" cy="90" r={radius} fill="none"
            stroke={scoreColor} strokeWidth="14"
            strokeDasharray={circumference}
            strokeDashoffset={dashOffset}
            strokeLinecap="round"
            transform="rotate(-90 90 90)"
            style={{ transition: 'stroke-dashoffset 0.8s ease' }}
          />
          <text x="90" y="95" textAnchor="middle" fontSize="30" fontWeight="800" fill={isDark ? '#ffffff' : '#111827'}>
            {Math.round(percentage)}%
          </text>
        </svg>
        <p className="score-verdict">{passed ? '🎉 Great job!' : '📚 Keep Practicing!'}</p>
        <p className="score-sub">You scored {correct} out of {total} correct</p>
      </div>

      {/* Stat Cards */}
      <div className="stat-cards-row">
        <div className="stat-card stat-card--correct">
          <div className="stat-card-icon">✓</div>
          <div className="stat-card-body">
            <span className="stat-card-label">Correct</span>
            <span className="stat-card-value">{correct}</span>
          </div>
        </div>
        <div className="stat-card stat-card--incorrect">
          <div className="stat-card-icon">✗</div>
          <div className="stat-card-body">
            <span className="stat-card-label">Incorrect</span>
            <span className="stat-card-value">{incorrect}</span>
          </div>
        </div>
        <div className="stat-card stat-card--time">
          <div className="stat-card-icon">⏱</div>
          <div className="stat-card-body">
            <span className="stat-card-label">Time Taken</span>
            <span className="stat-card-value">
              {Math.floor(timeTaken / 60)}:{String(timeTaken % 60).padStart(2, '0')}
            </span>
          </div>
        </div>
      </div>

      {/* Level Progression */}
      {results.current_difficulty !== 'hard' && (
        <div className={`level-progression-card ${results.can_progress ? 'can-progress' : 'needs-retry'}`}>
          <div className="progression-header">
            <Award size={32} color={results.can_progress ? '#f59e0b' : '#9333ea'} />
            <div>
              {results.can_progress ? (
                <>
                  <h3>🎉 Ready for Next Level!</h3>
                  <p>You passed! Move on to <strong>{results.next_difficulty}</strong> difficulty</p>
                </>
              ) : (
                <>
                  <h3>📚 Keep Practicing!</h3>
                  <p>Score 50% or more to unlock the next level</p>
                </>
              )}
            </div>
          </div>

          <div className="progression-path">
            {['easy', 'medium', 'hard'].map((level, idx, arr) => (
              <React.Fragment key={level}>
                <div className={`level-step ${getLevelStepClass(level)}`}>
                  <div className="level-icon" style={{ background: getDifficultyColor(level) }}>
                    {getDifficultyIcon(level)}
                  </div>
                  <span>{level.charAt(0).toUpperCase() + level.slice(1)}</span>
                  {getLevelStepClass(level) === 'completed' && <span className="level-check">✓</span>}
                  {getLevelStepClass(level) === 'current'   && <span className="level-next-label">Next</span>}
                </div>
                {idx < arr.length - 1 && (
                  <div className={`level-connector ${getLevelStepClass(level) === 'completed' ? 'active' : ''}`} />
                )}
              </React.Fragment>
            ))}
          </div>

          {results.can_progress ? (
            <button className="next-level-btn" onClick={onNextLevel} disabled={isGenerating}>
              <TrendingUp size={20} />
              <span>
                {isGenerating
                  ? 'Generating…'
                  : `Progress to ${results.next_difficulty?.charAt(0).toUpperCase() + results.next_difficulty?.slice(1)} Level →`}
              </span>
            </button>
          ) : (
            <button
              className="retry-level-btn"
              onClick={() => onRetryLevel(results.source_content, results.current_difficulty)}
              disabled={isGenerating}
            >
              <Sparkles size={20} />
              <span>
                {isGenerating
                  ? 'Generating…'
                  : `Retry ${results.current_difficulty?.charAt(0).toUpperCase() + results.current_difficulty?.slice(1)} Level`}
              </span>
            </button>
          )}
        </div>
      )}

      {/* Hard level outcomes */}
      {results.current_difficulty === 'hard' && results.passed && (
        <div className="completion-card">
          <Award size={48} color="#10b981" />
          <h3>🏆 All Levels Completed!</h3>
          <p>Congratulations — you mastered all three difficulty levels!</p>
        </div>
      )}

      {results.current_difficulty === 'hard' && !results.passed && (
        <div className="level-progression-card needs-retry">
          <div className="progression-header">
            <Award size={32} color="#9333ea" />
            <div>
              <h3>📚 Almost There!</h3>
              <p>Score 50% or more to complete the Hard level</p>
            </div>
          </div>
          <button
            className="retry-level-btn"
            onClick={() => onRetryLevel(results.source_content, 'hard')}
            disabled={isGenerating}
          >
            <Sparkles size={20} />
            <span>{isGenerating ? 'Generating…' : 'Retry Hard Level'}</span>
          </button>
        </div>
      )}

      {/* Actions */}
      <div className="results-actions">
        <button className="action-btn primary" onClick={onToggleReview}>
          <FileText size={20} />
          {showReview ? 'Hide Review' : 'Review Answers'}
        </button>
        <button className="action-btn secondary" onClick={onDownloadPDF}>
          <Download size={20} />
          Download as PDF
        </button>
        <button className="action-btn secondary" onClick={onShowHistory}>
          <BarChart3 size={20} />
          History &amp; Analytics
        </button>
        <button className="action-btn secondary" onClick={onRestart}>
          <Sparkles size={20} />
          New Topic
        </button>
      </div>

      {/* Answer Review */}
      {showReview && results.detailed_results && (
        <div className="review-section">
          <h3>Answer Review</h3>
          {results.detailed_results.map((item, index) => (
            <div
              key={index}
              className={`review-item ${
                item.is_correct === true  ? 'correct'      :
                item.is_correct === false ? 'incorrect'    : 'needs-review'
              }`}
            >
              <div className="review-header">
                <span className="review-number">Question {index + 1}</span>
                <span className={`review-badge ${
                  item.is_correct === true  ? 'correct'   :
                  item.is_correct === false ? 'incorrect' : 'needs-review'
                }`}>
                  {item.is_correct === true  ? 'Correct' :
                   item.is_correct === false ? 'Incorrect' : 'Needs Review'}
                </span>
                {item.question_type === 'short_answer' && (
                  <span className="type-badge">Short Answer</span>
                )}
              </div>
              <p className="review-question">{item.question_text}</p>
              <div className="review-answers">
                <div className="answer-row">
                  <span className="answer-label">Your Answer:</span>
                  <span className={
                    item.is_correct === true  ? 'correct-text'   :
                    item.is_correct === false ? 'incorrect-text' : 'review-text'
                  }>
                    {item.question_type === 'multiple_choice'
                      ? `${item.user_answer}. ${item.user_answer_text}`
                      : item.user_answer_text}
                  </span>
                </div>
                {item.is_correct === false && (
                  <div className="answer-row">
                    <span className="answer-label">Correct Answer:</span>
                    <span className="correct-text">
                      {item.question_type === 'multiple_choice'
                        ? `${item.correct_answer}. ${item.correct_answer_text}`
                        : item.correct_answer_text}
                    </span>
                  </div>
                )}
                {item.is_correct === null && (
                  <div className="answer-row">
                    <span className="answer-label">Expected Answer:</span>
                    <span className="review-text">{item.correct_answer_text}</span>
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

export default QuizResults;
