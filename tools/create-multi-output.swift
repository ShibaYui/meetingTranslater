#!/usr/bin/env swift
// Create a macOS "Multi-Output Device" (stacked aggregate device) that sends the same audio to your
// speakers/headphones AND to BlackHole, then make it the default sound output. Equivalent to doing it by
// hand in Audio MIDI Setup. Requires Xcode or the Command Line Tools (for the `swift` interpreter).
//
//   swift tools/create-multi-output.swift                      # speakers/headphones (current default) + BlackHole
//   swift tools/create-multi-output.swift --primary "MacBook Pro Speakers" --secondary "BlackHole 2ch"
//   swift tools/create-multi-output.swift --name "Meeting Output" --no-default
//   swift tools/create-multi-output.swift --remove             # delete the device created by this script
//
// The device is persistent (it survives reboots) and can also be deleted in Audio MIDI Setup.

import CoreAudio
import Foundation

let aggregateUID = "meetingtranslater.multi-output"
var deviceName = "Multi-Output Device"
var primaryQuery: String? = nil
var secondaryQuery = "BlackHole"
var setDefault = true
var remove = false

var args = Array(CommandLine.arguments.dropFirst())
while !args.isEmpty {
    let a = args.removeFirst()
    switch a {
    case "--name": deviceName = args.removeFirst()
    case "--primary": primaryQuery = args.removeFirst()
    case "--secondary": secondaryQuery = args.removeFirst()
    case "--no-default": setDefault = false
    case "--remove": remove = true
    default:
        FileHandle.standardError.write("unknown argument \(a)\n".data(using: .utf8)!)
        exit(2)
    }
}

let systemObject = AudioObjectID(kAudioObjectSystemObject)

func address(_ selector: AudioObjectPropertySelector,
             _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
}

func stringProperty(_ id: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
    var addr = address(selector)
    var value: CFString? = nil
    var size = UInt32(MemoryLayout<CFString?>.size)
    let status = withUnsafeMutablePointer(to: &value) { ptr in
        AudioObjectGetPropertyData(id, &addr, 0, nil, &size, ptr)
    }
    guard status == noErr, let v = value else { return nil }
    return v as String
}

func uint32Property(_ id: AudioObjectID, _ selector: AudioObjectPropertySelector) -> UInt32? {
    var addr = address(selector)
    var value: UInt32 = 0
    var size = UInt32(MemoryLayout<UInt32>.size)
    guard AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &value) == noErr else { return nil }
    return value
}

func allDevices() -> [AudioDeviceID] {
    var addr = address(kAudioHardwarePropertyDevices)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(systemObject, &addr, 0, nil, &size) == noErr else { return [] }
    var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
    guard AudioObjectGetPropertyData(systemObject, &addr, 0, nil, &size, &ids) == noErr else { return [] }
    return ids
}

func hasOutput(_ id: AudioDeviceID) -> Bool {
    var addr = address(kAudioDevicePropertyStreams, kAudioObjectPropertyScopeOutput)
    var size: UInt32 = 0
    return AudioObjectGetPropertyDataSize(id, &addr, 0, nil, &size) == noErr && size > 0
}

func isAggregate(_ id: AudioDeviceID) -> Bool {
    uint32Property(id, kAudioDevicePropertyTransportType) == kAudioDeviceTransportTypeAggregate
}

func defaultOutput() -> AudioDeviceID? {
    var addr = address(kAudioHardwarePropertyDefaultOutputDevice)
    var id: AudioDeviceID = 0
    var size = UInt32(MemoryLayout<AudioDeviceID>.size)
    guard AudioObjectGetPropertyData(systemObject, &addr, 0, nil, &size, &id) == noErr, id != 0 else { return nil }
    return id
}

func setDefaultOutput(_ id: AudioDeviceID) -> Bool {
    var addr = address(kAudioHardwarePropertyDefaultOutputDevice)
    var dev = id
    return AudioObjectSetPropertyData(systemObject, &addr, 0, nil, UInt32(MemoryLayout<AudioDeviceID>.size), &dev) == noErr
}

struct Device {
    let id: AudioDeviceID
    let uid: String
    let name: String
}

let devices: [Device] = allDevices().compactMap { id in
    guard let uid = stringProperty(id, kAudioDevicePropertyDeviceUID) else { return nil }
    let name = stringProperty(id, kAudioObjectPropertyName) ?? uid
    return Device(id: id, uid: uid, name: name)
}

let existing = devices.first { $0.uid == aggregateUID }

if remove {
    guard let dev = existing else {
        print("No device with UID \(aggregateUID) found; nothing to remove.")
        exit(0)
    }
    // Hand the default output back to a real device before destroying the aggregate.
    if defaultOutput() == dev.id,
       let fallback = devices.first(where: { hasOutput($0.id) && !isAggregate($0.id) && !$0.name.lowercased().contains("blackhole") }) {
        _ = setDefaultOutput(fallback.id)
        print("Default output restored to \(fallback.name)")
    }
    let status = AudioHardwareDestroyAggregateDevice(dev.id)
    guard status == noErr else {
        FileHandle.standardError.write("AudioHardwareDestroyAggregateDevice failed: \(status)\n".data(using: .utf8)!)
        exit(1)
    }
    print("Removed \(dev.name)")
    exit(0)
}

let outputs = devices.filter { hasOutput($0.id) }
guard let secondary = outputs.first(where: { $0.name.lowercased().contains(secondaryQuery.lowercased()) }) else {
    FileHandle.standardError.write("No output device matching \"\(secondaryQuery)\". Is BlackHole installed? (brew install --cask blackhole-2ch, then: sudo killall coreaudiod)\n".data(using: .utf8)!)
    exit(1)
}

let realOutputs = outputs.filter { !isAggregate($0.id) && $0.id != secondary.id && !$0.name.lowercased().contains("blackhole") }
var primary: Device? = nil
if let q = primaryQuery {
    primary = realOutputs.first { $0.name.lowercased().contains(q.lowercased()) }
    if primary == nil {
        FileHandle.standardError.write("No output device matching \"\(q)\". Available: \(realOutputs.map { $0.name })\n".data(using: .utf8)!)
        exit(1)
    }
} else if let current = defaultOutput(), let d = realOutputs.first(where: { $0.id == current }) {
    primary = d  // whatever you are listening on right now (speakers, headphones, AirPods)
} else {
    primary = realOutputs.first { $0.name.lowercased().contains("headphone") }
        ?? realOutputs.first { $0.name.lowercased().contains("speaker") }
        ?? realOutputs.first
}
guard let primaryDevice = primary else {
    FileHandle.standardError.write("No speaker/headphone output device found.\n".data(using: .utf8)!)
    exit(1)
}

var aggregateID: AudioDeviceID
if let dev = existing {
    print("Reusing existing device \(dev.name) (UID \(dev.uid)). Delete it with --remove to rebuild with other members.")
    aggregateID = dev.id
} else {
    let description: [String: Any] = [
        "name": deviceName,
        "uid": aggregateUID,
        "subdevices": [
            ["uid": primaryDevice.uid],
            ["uid": secondary.uid, "drift": 1],
        ],
        "master": primaryDevice.uid,
        "stacked": 1,   // stacked = Multi-Output Device (mirror), not a multi-channel aggregate
        "private": 0,   // visible to every app and persistent
    ]
    var newID: AudioDeviceID = 0
    let status = AudioHardwareCreateAggregateDevice(description as CFDictionary, &newID)
    guard status == noErr else {
        FileHandle.standardError.write("AudioHardwareCreateAggregateDevice failed: \(status)\n".data(using: .utf8)!)
        exit(1)
    }
    aggregateID = newID
    print("Created \"\(deviceName)\" = \(primaryDevice.name) + \(secondary.name) (drift correction on \(secondary.name))")
}

if setDefault {
    // Give coreaudiod a moment to publish the new device before selecting it.
    Thread.sleep(forTimeInterval: 0.5)
    if setDefaultOutput(aggregateID) {
        print("Default sound output is now \"\(deviceName)\". Volume keys will not work on it; adjust \(primaryDevice.name) in Audio MIDI Setup.")
    } else {
        print("Device created, but setting it as default output failed. Pick it in System Settings > Sound > Output.")
    }
}
