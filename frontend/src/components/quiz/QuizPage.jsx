import React, { useState, useEffect, useRef, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '@/hooks/useAuth';
import { getAccessToken } from '@/utils/tokenStorage';
import { API } from '@/config/api';
import { workspaceApi } from '@/services/workspaceApi';

import QuizHomePage from './QuizHomePage';
import QuizTaking   from './QuizTaking';
import QuizResults  from './QuizResults';
import QuizHistory  from './QuizHistory';
import Toast        from './Toast';
import ConfirmDialog from './ConfirmDialog';
import './QuizPage.css';

const getAuthHeaders = () => {
  const token = getAccessToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
};

const MAX_FILES    = 20;
const MAX_FILE_SIZE = 100 * 1024 * 1024;
const SUPPORTED_EXTENSIONS = new Set([
  'pdf', 'doc', 'docx', 'txt', 'md', 'rtf',
  'xlsx', 'xls', 'ppt', 'pptx',
  'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'tiff',
  'epub',
]);

const QuizPage = ({ noteId, onStepChange }) => {
  const { user, isLoading } = useAuth();
  const navigate = useNavigate();

  // Guests allowed — no login redirect
  const [step, setStep]               = useState('upload');
  useEffect(() => { onStepChange?.(step); }, [step]); // eslint-disable-line
  const [showHistory, setShowHistory] = useState(false);
  const [uploadedFiles, setUploadedFiles] = useState([]);
  const [quiz,          setQuiz]          = useState(null);
  const [answers,       setAnswers]       = useState({});
  const [results,       setResults]       = useState(null);
  const [completedLevels, setCompletedLevels] = useState([]);
  const [showReview,    setShowReview]    = useState(false);
  const [currentQuestion, setCurrentQuestion] = useState(0);
  const [timeRemaining,   setTimeRemaining]   = useState(null);
  const [quizStartTime,   setQuizStartTime]   = useState(null);
  const [isGenerating, setIsGenerating] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [dragActive,   setDragActive]   = useState(false);
  const [toasts,       setToasts]       = useState([]);
  const [dialog,       setDialog]       = useState({ isOpen: false });
  const [config, setConfig] = useState({
    numQuestions: 5, difficulty: 'easy', timeLimit: 30, questionType: 'mixed', contentFocus: 'both',
  });

  const isGeneratingRef      = useRef(false);
  const generationPromiseRef = useRef(null);
  const handleAutoSubmitRef  = useRef(null);
  useEffect(() => { handleAutoSubmitRef.current = handleAutoSubmit; });

  const showToast  = useCallback((message, type = 'error', duration = 5000) => {
    const id = Date.now();
    setToasts(prev => [...prev, { id, message, type, duration }]);
  }, []);
  const removeToast = useCallback((id) => setToasts(prev => prev.filter(t => t.id !== id)), []);
  const closeDialog = () => setDialog({ isOpen: false });

  useEffect(() => {
    try {
      const saved = sessionStorage.getItem('neuranote_quiz_state');
      if (saved) {
        const s = JSON.parse(saved);
        // Guard: 'results' step is only meaningful if we actually have the results payload to show
        const restoredStep = s.step === 'results' && !s.results ? 'upload' : s.step;

        if (restoredStep && restoredStep !== 'taking') setStep(restoredStep);
        if (s.currentQuestion !== undefined) setCurrentQuestion(s.currentQuestion);
        if (s.completedLevels)  setCompletedLevels(s.completedLevels);
        if (s.config)           setConfig(s.config);
        if (s.showHistory !== undefined) setShowHistory(s.showHistory);
        if (restoredStep === 'results') {
          if (s.quiz)    setQuiz(s.quiz);
          if (s.results) setResults(s.results);
          if (s.answers) setAnswers(s.answers);
          if (s.showReview !== undefined) setShowReview(s.showReview);
        }
      }
    } catch (_) {}
  }, []); // eslint-disable-line

  useEffect(() => {
    try {
      sessionStorage.setItem('neuranote_quiz_state', JSON.stringify({
        step, currentQuestion, config, completedLevels, showHistory, showReview,
        quizId: quiz?.quiz_id || null,
        // Only persist the (potentially large) quiz/results/answers payloads while actually on the results step
        quiz:    step === 'results' ? quiz    : null,
        results: step === 'results' ? results : null,
        answers: step === 'results' ? answers : null,
      }));
    } catch (_) {}
  }, [step, currentQuestion, config, completedLevels, showHistory, showReview, quiz, results, answers]);

  useEffect(() => {
    if (step !== 'taking') return;
    const handler = (e) => { e.preventDefault(); e.returnValue = 'Quiz in progress.'; return e.returnValue; };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [step]);

  useEffect(() => {
    if (step !== 'taking' || timeRemaining === null || timeRemaining <= 0) return;
    const timer = setInterval(() => {
      setTimeRemaining(prev => {
        if (prev <= 1) { clearInterval(timer); handleAutoSubmitRef.current?.(); return 0; }
        return prev - 1;
      });
    }, 1000);
    return () => clearInterval(timer);
  }, [step]); // eslint-disable-line

  const handleFiles = (fileList) => {
    const files = Array.from(fileList);
    if (uploadedFiles.length + files.length > MAX_FILES) {
      showToast(`Cannot upload more than ${MAX_FILES} files.`, 'error'); return;
    }
    // Files already sitting in the upload section, keyed by name + size so two different files that happen to share a name aren't treated as duplicates.
    const existingKeys = new Set(uploadedFiles.map(f => `${f.name.toLowerCase()}::${f.file?.size ?? ''}`));
    const seenInThisBatch = new Set();
    const validFiles = files.filter(file => {
      // Use lastIndexOf so filenames
      const lastDot = file.name.lastIndexOf('.');
      const rawExt = lastDot !== -1 ? file.name.slice(lastDot + 1).trim().toLowerCase() : '';
      const isValidExt = rawExt.length > 0 && rawExt.length <= 5 && !rawExt.includes(' ')
        && SUPPORTED_EXTENSIONS.has(rawExt);
      if (!isValidExt) { showToast(`"${file.name}" has an unsupported file type.`, 'error'); return false; }
      if (file.size > MAX_FILE_SIZE) { showToast(`"${file.name}" exceeds 100MB`, 'error'); return false; }

      const key = `${file.name.toLowerCase()}::${file.size}`;
      if (existingKeys.has(key) || seenInThisBatch.has(key)) {
        showToast('File already exists', 'error');
        return false;
      }
      seenInThisBatch.add(key);
      return true;
    });
    setUploadedFiles(prev => [...prev, ...validFiles.map(f => ({
      id: Date.now() + Math.random(), name: f.name,
      size: (f.size / (1024 * 1024)).toFixed(2) + ' MB', type: f.type, file: f,
    }))]);
    if (validFiles.length) showToast(`Uploaded ${validFiles.length} file(s)`, 'success', 3000);
  };

  const handleRemoveFile = (id) => { setUploadedFiles(prev => prev.filter(f => f.id !== id)); showToast('File removed', 'info', 2000); };
  const handleDrag = (e) => {
    e.preventDefault();
    e.stopPropagation();
    const types = e.dataTransfer?.types || [];
    if (types.includes('neuranote-quiz-file') || types.includes('Files')) {
      setDragActive(e.type === 'dragenter' || e.type === 'dragover');
    }
  };

  // Fetch a file blob for the given workspace file, routed through the backend
  const fetchFileBlob = async (fileId, displayName) => {
    showToast(`Loading "${displayName}"…`, 'info', 2000);
    try {
      return await workspaceApi.getFileContent(fileId);
    } catch (err) {
      // Fall back to the old preview-URL flow in case the content route is
      // unavailable (e.g. an older deployed backend), so uploads still work.
      const data          = await workspaceApi.getFilePreview(fileId);
      const previewObj    = data?.preview || data || {};
      const signedUrl     = previewObj?.preview_url;
      const inlineContent = previewObj?.content;

      if (signedUrl) {
        const res = await fetch(signedUrl);
        if (!res.ok) throw new Error(`Server returned ${res.status}`);
        return res.blob();
      }

      if (inlineContent) {
        const text = typeof inlineContent === 'string' && inlineContent.startsWith('data:')
          ? atob(inlineContent.split(',')[1])
          : inlineContent;
        return new Blob([text], { type: 'text/plain' });
      }

      throw err;
    }
  };

  // Resolve a safe filename for a folder-tree file drop.
  const resolveDropFileName = (fileName, fileType, content, blobMime) => {
    const originalName = String(fileName || 'file').trim() || 'file';
    const originalExt = originalName.includes('.')
      ? originalName.slice(originalName.lastIndexOf('.') + 1).toLowerCase()
      : '';

    // Preserve markdown/RTF extensions when the folder API supplies inline text.
    if (content) {
      if (['txt', 'md', 'rtf'].includes(originalExt)) return originalName;
      const base = originalName.replace(/\.[^.]+$/, '');
      return `${base}.txt`;
    }

    const GENERIC = ['file', '', null, undefined];
    const rawType = (fileType || '').toLowerCase();

    if (!GENERIC.includes(rawType)) {
      // Real type like 'pdf', 'docx', 'pptx' — append only if not already there
      const ext = `.${rawType}`;
      return originalName.toLowerCase().endsWith(ext) ? originalName : `${originalName}${ext}`;
    }

    // fileType is generic — try sniffing from the fileName itself
    const dotIdx = originalName.lastIndexOf('.');
    if (dotIdx !== -1) {
      const sniffed = originalName.slice(dotIdx + 1).toLowerCase();
      if (sniffed.length > 0 && sniffed.length <= 5 && !sniffed.includes(' ')) {
        return originalName; // already has a real extension
      }
    }

    // Fall back to blob mime type
    const mimeToExt = {
      'application/pdf': 'pdf',
      'application/msword': 'doc',
      'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
      'application/vnd.ms-powerpoint': 'ppt',
      'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'pptx',
      'application/vnd.ms-excel': 'xls',
      'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'xlsx',
      'text/plain': 'txt',
      'image/jpeg': 'jpg',
      'image/png': 'png',
      'image/gif': 'gif',
      'image/webp': 'webp',
    };
    if (blobMime && mimeToExt[blobMime]) return `${originalName}.${mimeToExt[blobMime]}`;

    // Last resort: treat as txt so at least something goes through
    return `${originalName}.txt`;
  };

  // File types whose extracted text cannot be trusted as a substitute for the real bytes.
  const BINARY_FILE_TYPES = ['pdf', 'doc', 'docx', 'ppt', 'pptx', 'xls', 'xlsx', 'epub'];

  const isBinaryFileType = (fileType, fileName) => {
    const rawType = (fileType || '').toLowerCase();
    if (BINARY_FILE_TYPES.includes(rawType)) return true;
    const ext = fileName && fileName.includes('.')
      ? fileName.slice(fileName.lastIndexOf('.') + 1).toLowerCase()
      : '';
    return BINARY_FILE_TYPES.includes(ext);
  };

  const handleDrop = async (e) => {
    e.preventDefault();
    e.stopPropagation();
    setDragActive(false);

    // Folder-panel drag
    const raw = e.dataTransfer.getData('neuranote-quiz-file');
    if (raw) {
      let fileData;
      try { fileData = JSON.parse(raw); } catch (_) { showToast('Invalid drag data.', 'error'); return; }

      const { fileId, fileName, fileType, fileUrl, content } = fileData;
      if (!fileId) { showToast('Could not identify dragged file.', 'error'); return; }

      // CRITICAL: for binary file types (PDF, DOCX, PPTX, etc.), never use the inline `content` text
      const shouldFetchRealBytes = isBinaryFileType(fileType, fileName);

      // fullName resolved after we know blob.type — placeholder for now
      let fullName = fileName;

      try {
        let blob;

        if (content && !shouldFetchRealBytes) {
          // Plain text/extracted content — no network needed
          blob = new Blob([content], { type: 'text/plain' });

        } else if (fileUrl && fileUrl.startsWith('data:')) {
          // Inline data-URI — decode it
          const [meta, b64] = fileUrl.split(',');
          const mime  = meta.split(':')[1].split(';')[0];
          const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
          blob = new Blob([bytes], { type: mime });

        } else {
          blob = await fetchFileBlob(fileId, fileName);
        }

        // For binary files, don't pass `content` to resolveDropFileName
        const effectiveContent = shouldFetchRealBytes ? null : content;
        fullName = resolveDropFileName(fileName, fileType, effectiveContent, blob.type);
        const file = new File([blob], fullName, { type: blob.type || 'application/octet-stream' });
        handleFiles([file]);
      } catch (err) {
        showToast(`Failed to load "${fullName}": ${err.message}`, 'error');
      }
      return;
    }

    // OS file drag
    if (e.dataTransfer.files?.[0]) {
      handleFiles(e.dataTransfer.files);
    }
  };

  const handleFolderFileDrop = async (fileData) => {
    const { fileId, fileName, fileType, fileUrl, content } = fileData || {};
    if (!fileId) { showToast('Could not identify dragged file.', 'error'); return; }

    // CRITICAL: for binary file types (PDF, DOCX, PPTX, etc.), never use the inline `content` text
    const shouldFetchRealBytes = isBinaryFileType(fileType, fileName);

    try {
      let blob;

      if (content && !shouldFetchRealBytes) {
        blob = new Blob([content], { type: 'text/plain' });

      } else if (fileUrl && fileUrl.startsWith('data:')) {
        const [meta, b64] = fileUrl.split(',');
        const mime  = meta.split(':')[1].split(';')[0];
        const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
        blob = new Blob([bytes], { type: mime });

      } else {
        blob = await fetchFileBlob(fileId, fileName);
      }

      // For binary files, don't pass `content` to resolveDropFileName
      const effectiveContent = shouldFetchRealBytes ? null : content;
      const fullName = resolveDropFileName(fileName, fileType, effectiveContent, blob.type);
      const file = new File([blob], fullName, { type: blob.type || 'application/octet-stream' });
      handleFiles([file]);
    } catch (err) {
      showToast(`Failed to load "${fileName}": ${err.message}`, 'error');
    }
  };

  const handleGenerateQuiz = async (levelSourceContent = null, forceDifficulty = null) => {
    if (isGeneratingRef.current || generationPromiseRef.current) return generationPromiseRef.current;
    if (!levelSourceContent && uploadedFiles.length === 0) { showToast('Please upload at least one file', 'error'); return; }
    if (config.numQuestions > 100) { showToast('Max 100 questions', 'error'); return; }
    if (config.timeLimit < 1 || config.timeLimit > 180) { showToast('Time limit 1–180 minutes', 'error'); return; }

    isGeneratingRef.current = true;
    setIsGenerating(true);

    const promise = (async () => {
      try {
        const formData = new FormData();
        const diff = forceDifficulty || config.difficulty;
        if (levelSourceContent) {
          formData.append('source_content', levelSourceContent);
          formData.append('files', new Blob(['placeholder']), 'placeholder.txt');
        } else {
          uploadedFiles.forEach(f => formData.append('files', f.file));
        }
        formData.append('num_questions', config.numQuestions);
        formData.append('difficulty', diff);
        formData.append('time_limit', config.timeLimit);
        formData.append('question_type', config.questionType);
        formData.append('content_focus', config.contentFocus);
        if (noteId) formData.append('note_id', noteId);

        const response = await fetch(API.generate, { method: 'POST', headers: getAuthHeaders(), body: formData });
        if (!response.ok) { const e = await response.json(); throw new Error(e.detail || 'Quiz generation failed'); }

        const data = await response.json();
        setQuiz(data); setTimeRemaining(data.time_limit * 60); setQuizStartTime(Date.now());
        if (forceDifficulty) setConfig(prev => ({ ...prev, difficulty: diff }));
        setStep('taking');
        showToast(`${diff.charAt(0).toUpperCase() + diff.slice(1)} quiz generated!`, 'success', 3000);
        return data;
      } catch (err) {
        showToast(err.message || 'Failed to generate quiz.', 'error');
        throw err;
      } finally {
        setIsGenerating(false); isGeneratingRef.current = false; generationPromiseRef.current = null;
      }
    })();

    generationPromiseRef.current = promise;
    return promise;
  };

  const handleAnswerSelect      = (v) => setAnswers(prev => ({ ...prev, [currentQuestion]: v }));
  const handleShortAnswerChange = (e) => setAnswers(prev => ({ ...prev, [currentQuestion]: e.target.value }));
  const handleNext              = ()  => setCurrentQuestion(q => Math.min(q + 1, quiz.questions.length - 1));
  const handlePrevious          = ()  => setCurrentQuestion(q => Math.max(q - 1, 0));
  const handleQuestionNavigate  = (i) => setCurrentQuestion(i);

  const _doSubmit = async () => {
    setIsSubmitting(true);
    try {
      const timeTaken = Math.floor((Date.now() - quizStartTime) / 1000);
      const formData  = new FormData();
      formData.append('answers', JSON.stringify(answers));
      formData.append('time_taken', timeTaken);

      const response = await fetch(API.submit(quiz.quiz_id), { method: 'POST', headers: getAuthHeaders(), body: formData });
      if (!response.ok) {
        let detail = 'Submission failed';
        try { const e = await response.json(); detail = e.detail || detail; } catch (_) {}
        throw new Error(detail);
      }

      const data = await response.json();
      if (quiz.difficulty && !completedLevels.includes(quiz.difficulty))
        setCompletedLevels(prev => [...prev, quiz.difficulty]);
      setResults(data); setStep('results');
      showToast('Quiz submitted successfully!', 'success', 3000);
    } catch (err) {
      showToast(`Failed to submit: ${err.message || 'Unknown error'}`, 'error');
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleSubmitQuiz = async (autoSubmit = false) => {
    const totalQ = quiz.questions.length;
    const answered = Object.values(answers).filter(v => v !== null && v !== undefined && String(v).trim() !== '').length;
    const unanswered = totalQ - answered;

    // Check long answer minimum word count
    if (!autoSubmit) {
      const shortLongAnswers = quiz.questions.reduce((acc, q, idx) => {
        if (q.question_type === 'long_answer') {
          const ans = answers[idx] || '';
          const wc = ans.trim() ? ans.trim().split(/\s+/).length : 0;
          if (ans.trim() && wc < 50) acc.push(idx + 1);
        }
        return acc;
      }, []);
      if (shortLongAnswers.length > 0) {
        showToast(
          `Question${shortLongAnswers.length > 1 ? 's' : ''} ${shortLongAnswers.join(', ')} require at least 50 words for the long answer.`,
          'error', 6000
        );
        return;
      }
    }

    if (autoSubmit) { await _doSubmit(); return; }
    setDialog({
      isOpen: true, type: unanswered > 0 ? 'unanswered' : 'submit',
      message: unanswered > 0
        ? `You have ${unanswered} unanswered question${unanswered > 1 ? 's' : ''}. Submit anyway?`
        : `All ${totalQ} questions answered. Submit?`,
      okLabel: unanswered > 0 ? 'Submit Anyway' : 'Submit Quiz',
      cancelLabel: 'Return to Quiz',
      onConfirm: () => { closeDialog(); _doSubmit(); },
    });
  };

  const handleAutoSubmit = async () => { showToast('Time is up! Submitting…', 'info', 3000); await handleSubmitQuiz(true); };

  const handleCancelQuiz = () => {
    setDialog({
      isOpen: true, type: 'cancel',
      message: 'Cancel quiz? All progress will be lost.',
      okLabel: 'Yes, Cancel Quiz', cancelLabel: 'Return to Quiz',
      onConfirm: () => {
        closeDialog(); setStep('upload'); setQuiz(null); setAnswers({});
        setCurrentQuestion(0); setTimeRemaining(null); setQuizStartTime(null);
        showToast('Quiz cancelled', 'info', 3000);
      },
    });
  };

  const handleNextLevel = async () => {
    if (!results?.can_progress) { showToast('All levels complete!', 'info'); return; }
    const cd = results.current_difficulty;
    if (cd && !completedLevels.includes(cd)) setCompletedLevels(prev => [...prev, cd]);
    const { next_difficulty, source_content: sc } = results;
    // Show loading overlay on the results page BEFORE clearing state
    setIsGenerating(true);
    // Small delay so React flushes the isGenerating=true render (shows overlay)
    await new Promise(r => setTimeout(r, 0));
    setAnswers({}); setCurrentQuestion(0); setResults(null); setShowReview(false);
    await handleGenerateQuiz(sc, next_difficulty);
  };

  const handleRetryLevel = async (sc, difficulty) => {
    // Show loading overlay on the results page BEFORE clearing state
    setIsGenerating(true);
    await new Promise(r => setTimeout(r, 0));
    setAnswers({}); setCurrentQuestion(0); setResults(null); setShowReview(false);
    setTimeRemaining(null); setQuizStartTime(null);
    await handleGenerateQuiz(sc, difficulty);
  };

  const handleDownloadPDF = async () => {
    try {
      const res = await fetch(API.pdf(quiz.quiz_id, results.attempt_id), { headers: getAuthHeaders() });
      if (!res.ok) throw new Error('PDF generation failed');
      const blob = await res.blob();
      const url  = window.URL.createObjectURL(blob);
      const a    = document.createElement('a');
      a.href = url; a.download = `quiz-results-${quiz.quiz_id}.pdf`;
      document.body.appendChild(a); a.click(); window.URL.revokeObjectURL(url); document.body.removeChild(a);
      showToast('PDF downloaded!', 'success', 3000);
    } catch (_) { showToast('Failed to download PDF.', 'error'); }
  };

  const handleRestart = () => {
    sessionStorage.removeItem('neuranote_quiz_state');
    setStep('upload'); setUploadedFiles([]); setQuiz(null); setAnswers({});
    setCurrentQuestion(0); setResults(null); setCompletedLevels([]);
    setConfig(prev => ({ ...prev, difficulty: 'easy' }));
    showToast('Starting new quiz', 'info', 2000);
  };

  const handleRetakeFromHistory = async (retakeConfig) => {
    // retakeConfig = { source_content, num_questions, difficulty, time_limit, question_type }
    setShowHistory(false);
    setAnswers({});
    setCurrentQuestion(0);
    setResults(null);
    setShowReview(false);
    setTimeRemaining(null);
    setQuizStartTime(null);
    setConfig(prev => ({
      ...prev,
      numQuestions: retakeConfig.num_questions,
      difficulty: retakeConfig.difficulty,
      timeLimit: retakeConfig.time_limit,
      questionType: retakeConfig.question_type,
    }));
    setIsGenerating(true);
    await new Promise(r => setTimeout(r, 0));
    try {
      const formData = new FormData();
      formData.append('source_content', retakeConfig.source_content);
      formData.append('files', new Blob(['placeholder']), 'placeholder.txt');
      formData.append('num_questions', retakeConfig.num_questions);
      formData.append('difficulty', retakeConfig.difficulty);
      formData.append('time_limit', retakeConfig.time_limit);
      formData.append('question_type', retakeConfig.question_type);
      formData.append('content_focus', config.contentFocus);

      const response = await fetch(API.generate, { method: 'POST', headers: getAuthHeaders(), body: formData });
      if (!response.ok) { const e = await response.json(); throw new Error(e.detail || 'Quiz generation failed'); }

      const data = await response.json();
      setQuiz(data);
      setTimeRemaining(data.time_limit * 60);
      setQuizStartTime(Date.now());
      setStep('taking');
      showToast('Retake quiz generated!', 'success', 3000);
    } catch (err) {
      showToast(err.message || 'Failed to generate retake quiz.', 'error');
      setStep('upload');
    } finally {
      setIsGenerating(false);
    }
  };

  if (showHistory) {
    return (
      <div className="quiz-page">
        <ToastLayer toasts={toasts} removeToast={removeToast} />
        <QuizHistory onBack={() => setShowHistory(false)} onRetakeQuiz={handleRetakeFromHistory} />
      </div>
    );
  }

  return (
    <div className="quiz-page">
      <ToastLayer toasts={toasts} removeToast={removeToast} />

      {step === 'upload' && (
        <QuizHomePage
          uploadedFiles={uploadedFiles}
          isGenerating={isGenerating}
          dragActive={dragActive}
          config={config}
          onFilesAdded={handleFiles}
          onRemoveFile={handleRemoveFile}
          onDrag={handleDrag}
          onDrop={handleDrop}
          onFolderFileDrop={handleFolderFileDrop}
          onGenerateQuiz={() => handleGenerateQuiz()}
          onShowHistory={() => setShowHistory(true)}
          onConfigChange={(partial) => setConfig(prev => ({ ...prev, ...partial }))}
          showToast={showToast}
        />
      )}

      {step === 'taking' && (
        <QuizTaking
          quiz={quiz}
          currentQuestion={currentQuestion}
          answers={answers}
          timeRemaining={timeRemaining}
          isSubmitting={isSubmitting}
          onAnswerSelect={handleAnswerSelect}
          onShortAnswerChange={handleShortAnswerChange}
          onNext={handleNext}
          onPrevious={handlePrevious}
          onQuestionNavigate={handleQuestionNavigate}
          onSubmitQuiz={handleSubmitQuiz}
          onCancelQuiz={handleCancelQuiz}
        />
      )}

      {step === 'results' && (
        <QuizResults
          results={results}
          quiz={quiz}
          completedLevels={completedLevels}
          isGenerating={isGenerating}
          showReview={showReview}
          onToggleReview={() => setShowReview(r => !r)}
          onDownloadPDF={handleDownloadPDF}
          onShowHistory={() => setShowHistory(true)}
          onRestart={handleRestart}
          onNextLevel={handleNextLevel}
          onRetryLevel={handleRetryLevel}
        />
      )}

      <ConfirmDialog
        isOpen={dialog.isOpen}
        type={dialog.type}
        message={dialog.message}
        okLabel={dialog.okLabel}
        cancelLabel={dialog.cancelLabel}
        onConfirm={dialog.onConfirm || closeDialog}
        onCancel={closeDialog}
      />
    </div>
  );
};

const ToastLayer = ({ toasts, removeToast }) => (
  <div className="toast-container">
    {toasts.map(t => (
      <Toast key={t.id} message={t.message} type={t.type} duration={t.duration} onClose={() => removeToast(t.id)} />
    ))}
  </div>
);

export default QuizPage;
