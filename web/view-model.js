(function exposeViewModel(root){
  function cleanDisplayText(raw){
    return String(raw || '')
      .replace(/<think\b[^>]*>[\s\S]*?<\/think\s*>/gi, '')
      .replace(/<think\b[^>]*>[\s\S]*$/gi, '')
      .replace(/^```[a-zA-Z]*\s*/i, '')
      .replace(/\s*```$/i, '')
      .trim();
  }

  function resourcesForKp(resources, kp){
    return (resources || []).filter(resource => resource.kp === kp);
  }

  function feedbackNextAction(path, currentKp, decision){
    if(decision && decision.action === 'advance'){
      const next = path[path.indexOf(currentKp) + 1];
      if(next) return {kind:'next', label:'继续下一知识点', targetKp:next};
      return {kind:'summary', label:'查看更新后的学习建议', targetKp:null};
    }
    return {kind:'repeat', label:'重新学习本知识点', targetKp:currentKp};
  }

  root.AgentEduView = Object.freeze({cleanDisplayText, resourcesForKp, feedbackNextAction});
})(typeof window === 'undefined' ? globalThis : window);
