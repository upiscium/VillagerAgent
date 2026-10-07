'use strict'

// Fixed, passive K11 capture. 360 means all horizontal directions are eligible;
// every candidate still requires a loaded, unobstructed supercover ray.
const OFFSETS = Object.freeze(
  Array.from({ length: 5 }, (_, x) => x - 2).flatMap(dx =>
    Array.from({ length: 3 }, (_, y) => y - 1).flatMap(dy =>
      Array.from({ length: 5 }, (_, z) => z - 2).map(dz =>
        Object.freeze({ x: dx, y: dy, z: dz })
      )
    )
  )
)
// Bound distinct voxels touched by the supercover, including start and target.
const MAX_STEPS = 16
const AIR_NAMES = new Set(['air', 'cave_air', 'void_air'])
const BLOCK_NAME = /^[a-z0-9_]+$/

function finitePosition(position) {
  return position && Number.isFinite(position.x) && Number.isFinite(position.y) &&
    Number.isFinite(position.z)
}

function positionKey(position) {
  return `${position.x},${position.y},${position.z}`
}

function supercoverPath(origin, target, maxTouchedVoxels) {
  const current = { x: Math.floor(origin.x), y: Math.floor(origin.y), z: Math.floor(origin.z) }
  const start = { ...current }
  const goal = { x: target.x, y: target.y, z: target.z }
  const delta = { x: target.x + 0.5 - origin.x, y: target.y + 0.5 - origin.y,
    z: target.z + 0.5 - origin.z }
  const step = { x: Math.sign(delta.x), y: Math.sign(delta.y), z: Math.sign(delta.z) }
  const tDelta = {}
  const tMax = {}

  for (const axis of ['x', 'y', 'z']) {
    if (step[axis] === 0) {
      tDelta[axis] = Infinity
      tMax[axis] = Infinity
    } else {
      tDelta[axis] = Math.abs(1 / delta[axis])
      const boundary = step[axis] > 0 ? current[axis] + 1 : current[axis]
      tMax[axis] = (boundary - origin[axis]) / delta[axis]
    }
  }

  const path = []
  const seen = new Set()

  function addTouched(position) {
    if (position.x === start.x && position.y === start.y && position.z === start.z) return true
    const key = positionKey(position)
    if (seen.has(key)) return true
    path.push(position)
    seen.add(key)
    // The start voxel plus every distinct touched neighbor counts as one step.
    return 1 + path.length <= maxTouchedVoxels
  }

  const originBoundaryAxes = ['x', 'y', 'z'].filter(axis => Number.isInteger(origin[axis]))
  // A ray may start exactly on a face/edge/corner. Include every voxel touched
  // at t=0 as well as the floor-selected eye voxel.
  for (let mask = 1; mask < (1 << originBoundaryAxes.length); mask += 1) {
    const touched = { ...current }
    for (let bit = 0; bit < originBoundaryAxes.length; bit += 1) {
      if (mask & (1 << bit)) touched[originBoundaryAxes[bit]] -= 1
    }
    if (!addTouched(touched)) return { path, truncated: true }
  }
  while (current.x !== goal.x || current.y !== goal.y || current.z !== goal.z) {
    const nextT = Math.min(tMax.x, tMax.y, tMax.z)
    const axes = ['x', 'y', 'z'].filter(axis => Math.abs(tMax[axis] - nextT) <= 1e-12)
    if (axes.length === 0 || !Number.isFinite(nextT)) return { path, error: true }

    // At edge/corner ties, every non-empty combination is touched by the ray.
    for (let mask = 1; mask < (1 << axes.length); mask += 1) {
      const touched = { ...current }
      for (let bit = 0; bit < axes.length; bit += 1) {
        if (mask & (1 << bit)) touched[axes[bit]] += step[axes[bit]]
      }
      if (!addTouched(touched)) return { path, truncated: true }
    }
    for (const axis of axes) {
      current[axis] += step[axis]
      tMax[axis] += tDelta[axis]
    }
  }
  return { path, truncated: false }
}

function createVisibleBlockCapture(options = {}) {
  const Vec3 = options.Vec3
  const mcData = options.mcData
  let captureSeq = 0
  let captureInProgress = false

  // This function deliberately contains no await, promise, timer, RPC, or action.
  // A call gathers the pose and all 75 cells synchronously in one Node turn.
  function captureVisibleBlockRegion(bot) {
    const thisCaptureSeq = ++captureSeq
    const started = process.hrtime.bigint().toString()
    let ended = started

    // A bot.blockAt spy/plugin may re-enter this closure. Fail closed before any
    // bot/property read, and let the outer call own the in-progress lock.
    if (captureInProgress) {
      const cells = OFFSETS.map(offset => ({
        offset: { ...offset },
        position: null,
        state: 'unknown',
        unknown_reason: 'incoherent_capture'
      }))
      ended = process.hrtime.bigint().toString()
      return JSON.stringify({
        capture_seq: thisCaptureSeq,
        pose: null,
        eye: null,
        capture_started_monotonic_ns: started,
        capture_ended_monotonic_ns: ended,
        cells,
        complete: false,
        truncated: false,
        error: 'incoherent_capture'
      })
    }

    captureInProgress = true
    let complete = true
    let truncated = false
    let error = null
    let pose = null
    let eye = null
    const cells = []
    const lookupCache = new Map()
    let lookupFailed = false

    function vector(x, y, z) {
      return typeof Vec3 === 'function' ? new Vec3(x, y, z) : { x, y, z }
    }

    function blockAt(position) {
      const key = positionKey(position)
      if (lookupCache.has(key)) return lookupCache.get(key)
      let block
      try {
        block = bot.blockAt(vector(position.x, position.y, position.z), false)
      } catch (_error) {
        lookupFailed = true
        lookupCache.set(key, undefined)
        return undefined
      }
      lookupCache.set(key, block == null ? null : block)
      return block == null ? null : block
    }

    function unknown(offset, position, reason) {
      return { offset: { ...offset }, position: position == null ? null : { ...position },
        state: 'unknown', unknown_reason: reason }
    }

    function identity(block) {
      if (!block || !Number.isInteger(block.type) || block.type < 0 ||
          typeof block.name !== 'string' || !BLOCK_NAME.test(block.name)) return null
      const registry = mcData && mcData.blocksByName && mcData.blocksByName[block.name]
      if (!registry || registry.id !== block.type || registry.name !== block.name) return null
      return { registry_id: block.type, block_name: block.name }
    }

    function proof(position, blockIdentity) {
      return { position: { ...position }, registry_id: blockIdentity.registry_id,
        block_name: blockIdentity.block_name }
    }

    function clearAsUnknown(reason, foot) {
      cells.length = 0
      for (const offset of OFFSETS) {
        const target = foot ? { x: foot.x + offset.x, y: foot.y + offset.y, z: foot.z + offset.z } : null
        cells.push(unknown(offset, target, reason))
      }
    }

    let initialPosition = null
    let initialEyeHeight = null
    let foot = null
    let eyeVoxel = null
    try {
      const entity = bot && bot.entity
      const position = entity && entity.position
      if (!finitePosition(position)) throw new Error('invalid_pose')
      initialPosition = { x: position.x, y: position.y, z: position.z }
      pose = { x: String(position.x), y: String(position.y), z: String(position.z) }
      const eyeHeight = entity.eyeHeight
      if (!Number.isFinite(eyeHeight) || eyeHeight <= 0) throw new Error('invalid_pose')
      initialEyeHeight = eyeHeight
      const eyePosition = { x: position.x, y: position.y + eyeHeight, z: position.z }
      if (!finitePosition(eyePosition)) throw new Error('invalid_pose')
      eye = { x: String(eyePosition.x), y: String(eyePosition.y), z: String(eyePosition.z),
        eye_height: String(eyeHeight) }
      foot = { x: Math.floor(position.x), y: Math.floor(position.y), z: Math.floor(position.z) }
      eyeVoxel = { x: Math.floor(eyePosition.x), y: Math.floor(eyePosition.y), z: Math.floor(eyePosition.z) }

      if (typeof bot.blockAt !== 'function') throw new Error('unloaded_path')
      const eyeBlock = blockAt(eyeVoxel)
      if (eyeBlock === null) {
        clearAsUnknown('unloaded_path', foot)
      } else if (eyeBlock === undefined) {
        complete = false
        error = 'unloaded_path'
        clearAsUnknown('unloaded_path', foot)
      } else {
        const eyeIdentity = identity(eyeBlock)
        if (!eyeIdentity) {
          clearAsUnknown('unmapped_registry', foot)
        } else if (!AIR_NAMES.has(eyeIdentity.block_name)) {
          // A block occupying the eye voxel is not a clear actor-local LOS.
          clearAsUnknown('occluded', foot)
        } else for (const offset of OFFSETS) {
          const target = { x: foot.x + offset.x, y: foot.y + offset.y, z: foot.z + offset.z }
          if (![-2, -1, 0, 1, 2].includes(offset.x) || ![-1, 0, 1].includes(offset.y)
              || ![-2, -1, 0, 1, 2].includes(offset.z)) {
            cells.push(unknown(offset, target, 'outside_region'))
            continue
          }
          const targetBlock = blockAt(target)
          if (targetBlock === null || targetBlock === undefined) {
            cells.push(unknown(offset, target, 'unloaded_target'))
            continue
          }
          const targetIdentity = identity(targetBlock)
          if (!targetIdentity) {
            cells.push(unknown(offset, target, 'unmapped_registry'))
            continue
          }
          if (target.x === eyeVoxel.x && target.y === eyeVoxel.y && target.z === eyeVoxel.z) {
            // The observer cannot establish visibility of a target in its own eye voxel.
            cells.push(unknown(offset, target, 'outside_region'))
            continue
          }

          const ray = supercoverPath(eyePosition, target, MAX_STEPS)
          if (ray.truncated) {
            truncated = true
            complete = false
            cells.push(unknown(offset, target, 'step_limit'))
            continue
          }
          if (ray.error) {
            complete = false
            error = error || 'incoherent_capture'
            cells.push(unknown(offset, target, 'incoherent_capture'))
            continue
          }

          let pathUnloaded = false
          let pathUnmapped = false
          let obstructed = false
          const pathProof = []
          for (const pathPosition of ray.path) {
            if ((pathPosition.x === target.x && pathPosition.y === target.y && pathPosition.z === target.z)
                || (pathPosition.x === eyeVoxel.x && pathPosition.y === eyeVoxel.y
                    && pathPosition.z === eyeVoxel.z)) continue
            const pathBlock = blockAt(pathPosition)
            if (pathBlock == null) {
              pathUnloaded = true
              continue
            }
            const pathIdentity = identity(pathBlock)
            if (!pathIdentity) {
              pathUnmapped = true
              continue
            }
            if (!AIR_NAMES.has(pathIdentity.block_name)) obstructed = true
            pathProof.push(proof(pathPosition, pathIdentity))
          }
          if (pathUnloaded) {
            cells.push(unknown(offset, target, 'unloaded_path'))
          } else if (pathUnmapped) {
            cells.push(unknown(offset, target, 'unmapped_registry'))
          } else if (obstructed) {
            cells.push(unknown(offset, target, 'occluded'))
          } else {
            const knownState = AIR_NAMES.has(targetIdentity.block_name) ? 'known_air' : 'known_non_air'
            cells.push({
              offset: { ...offset },
              position: { ...target },
              state: knownState,
              registry_id: targetIdentity.registry_id,
              block_name: targetIdentity.block_name,
              coverage: {
                eye_loaded: proof(eyeVoxel, eyeIdentity),
                target_loaded: proof(target, targetIdentity),
                path: pathProof
              }
            })
          }
        }
      }

      if (lookupFailed) {
        complete = false
        error = error || 'unloaded_path'
      }

      // Any pose drift makes this snapshot incoherent; invalidate the entire tick.
      const finalEntity = bot && bot.entity
      const finalPosition = finalEntity && finalEntity.position
      const finalEyeHeight = finalEntity && finalEntity.eyeHeight
      if (!finitePosition(finalPosition) || !Number.isFinite(finalEyeHeight)
          || finalPosition.x !== initialPosition.x || finalPosition.y !== initialPosition.y
          || finalPosition.z !== initialPosition.z || finalEyeHeight !== initialEyeHeight) {
        complete = false
        error = 'incoherent_capture'
        clearAsUnknown('incoherent_capture', foot)
      }
    } catch (captureError) {
      complete = false
      const reason = captureError && ['invalid_pose', 'unloaded_path'].includes(captureError.message)
        ? captureError.message : 'incoherent_capture'
      error = reason
      clearAsUnknown(reason, foot)
    } finally {
      try {
        ended = process.hrtime.bigint().toString()
      } finally {
        captureInProgress = false
      }
    }

    return JSON.stringify({
      capture_seq: thisCaptureSeq,
      pose,
      eye,
      capture_started_monotonic_ns: started,
      capture_ended_monotonic_ns: ended,
      cells,
      complete,
      truncated,
      error
    })
  }

  return captureVisibleBlockRegion
}

module.exports = {
  OFFSETS,
  MAX_STEPS,
  supercoverPath,
  createVisibleBlockCapture
}
